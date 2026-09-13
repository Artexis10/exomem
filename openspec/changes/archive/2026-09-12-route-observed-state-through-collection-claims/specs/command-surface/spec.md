## ADDED Requirements

### Requirement: Compiled and Evidence writes may return one advisory Records routing hint
When a compiled-note mutation or an Evidence preserve commits, the system SHALL compare the written page's terms with the effective claims of every routing-target collection the caller may read. When exactly one collection is covered at or above the minimum coverage and strictly more than any other, the successful result SHALL include at most one `records_routing` advisory, projected into the default compact terminal as a bounded top-level field, carrying the collection's manifest path and title, the sorted matched terms (at most six), the collection's declared natural-key field names, and a `strength` of exactly `strong` or `moderate`. It SHALL NOT report a numeric confidence. It SHALL name no page other than a manifest the caller may read. It is advisory: it SHALL NOT alter `status`, `mutated`, `path`, `warnings_count`, mutation identity or replay behaviour, SHALL be absent rather than null when nothing qualifies, and a failure in its computation SHALL cost the caller the advisory and never the write. The runtime SHALL NOT append a record as a consequence of the advisory.

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
