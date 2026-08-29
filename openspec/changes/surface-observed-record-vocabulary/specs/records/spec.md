## ADDED Requirements

### Requirement: Records inspection surfaces observed free-string vocabulary

Collection inspection SHALL report, for every declared string-typed field that declares no
`enum`, a bounded summary of the distinct values the authorized items already carry,
paired with each value's occurrence count. The summary SHALL be additive: every existing
inspection key keeps its shape and meaning.

Values SHALL be counted by their full text with surrounding whitespace removed, and empty
results SHALL be ignored. Counting SHALL NOT key on a shortened form: two distinct values
that share a prefix long enough to collide once shortened SHALL remain two entries with
their own counts.

The summary SHALL be bounded independently of collection size. At most 20 distinct values
per field SHALL be emitted, and when a field carries more, the retained values SHALL be the
most frequent ones, ranked by descending count and breaking ties by ascending value, so
that the cap drops the rarest terms rather than the ones the item pass happened to meet
last. A per-field truncation flag SHALL say so whenever the distinct-value cap binds.

Each emitted value SHALL be cut to a bounded display length and SHALL carry its own
always-present flag stating whether that cut applied. Fields declaring an `enum` SHALL NOT
be summarized, because the declaration is already the vocabulary.

The summary SHALL be derived from the same authorized item pass that produces the rest of
the inspection payload and SHALL carry the same serve-time filtering, under the same
path-granular authorization that pass already applies. Neither a value nor a count SHALL
reflect an item the requesting audience may not read. Inspection SHALL NOT perform an
additional or unbounded scan to produce it, and SHALL omit the summary entirely when no
item pass ran.

#### Scenario: Free-string field reveals the vocabulary already in use
- **GIVEN** a collection whose manifest declares a free-string field and whose items carry
  three or more distinct values for it
- **WHEN** a client inspects the collection
- **THEN** the response reports each distinct value with its occurrence count and with the
  flag stating that no display cut applied
- **AND** an appending agent can reuse an existing term instead of echoing the user

#### Scenario: Capped field keeps its most frequent terms
- **WHEN** a free-string field carries more distinct values than the cap admits
- **THEN** inspection reports exactly the capped number of distinct values, most frequent
  first, with ties broken by ascending value
- **AND** a term seen many times late in the item pass is retained over a term seen once
- **AND** the field's summary is flagged as truncated rather than silently partial

#### Scenario: Long values stay distinct and say they were cut
- **WHEN** two distinct values share a prefix long enough to collide at the display length
- **THEN** inspection reports two entries, each with its own count
- **AND** each entry is flagged as display-truncated, even though their emitted text matches

#### Scenario: Declared enum is not restated as observed usage
- **WHEN** a manifest declares a field as `enum`
- **THEN** inspection reports no observed-value summary for that field
- **AND** the declared values remain discoverable through the authoring contract

#### Scenario: Withheld item contributes neither a value nor a count
- **GIVEN** governance withholds one item of an otherwise released collection
- **WHEN** a client of that audience inspects the collection
- **THEN** a value occurring only on the withheld item is absent from the summary
- **AND** a value the withheld item shares with released items is counted from the released
  items alone
- **AND** the counts, the truncation flags, and the rest of the payload disclose no trace of
  that value or of the item's existence

#### Scenario: Unreadable collection claims no sweep
- **WHEN** inspection cannot parse the collection's canonical items
- **THEN** the response omits the observed-value summary entirely rather than reporting an
  empty one, which would claim a sweep that never ran
