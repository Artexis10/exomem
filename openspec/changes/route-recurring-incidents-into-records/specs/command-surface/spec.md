## MODIFIED Requirements

### Requirement: Compiled and Evidence writes may return one advisory Records routing hint
When a compiled-note mutation or an Evidence preserve commits, the system SHALL compare the written page's terms with the effective claims of every routing-target collection the caller may read. The written page's terms SHALL include its title, page tags, unit tags, `type`, categories (page and unit) and projects (`project` and `projects`). When exactly one routing-target collection declares `claims.match` predicates that all hold on the page, that collection SHALL win with strength `strong` regardless of word coverage, carrying the sorted `matched_predicates`; when several do, the one with strictly the highest coverage wins and a tie SHALL stay silent; when none does, coverage routing applies unchanged. Otherwise, when exactly one collection is covered at or above the minimum coverage and strictly more than any other, the successful result SHALL include at most one `records_routing` advisory, projected into the default compact terminal as a bounded top-level field, carrying the collection's manifest path and title, the sorted matched terms (at most six), the collection's declared natural-key field names, and a `strength` of exactly `strong` or `moderate`. It SHALL NOT report a numeric confidence. It SHALL name no page other than a manifest the caller may read. It is advisory: it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity or replay behaviour, SHALL be absent rather than null when nothing qualifies, and a failure in its computation SHALL cost the caller the advisory and never the write. The runtime SHALL NOT append a record as a consequence of the advisory.

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
- **THEN** the disposition is `append_occurrence` naming that item's key and the note to add to its sources, not a new item

#### Scenario: A moderate route keeps the plain advisory
- **WHEN** the route is `moderate` or the observation is not failure-shaped
- **THEN** the advisory carries no disposition

#### Scenario: A malformed disposition is dropped whole
- **WHEN** the advisory carries an unknown disposition, a payload for another collection or action, an oversized payload or question, or an unknown key
- **THEN** the compact terminal carries no `records_routing` and the write is unchanged
