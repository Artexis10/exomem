## MODIFIED Requirements

### Requirement: One-Hop Wikilink Neighbourhood Ranked By Co-Citation

The system SHALL assemble the `neighborhood` as the 1-hop inbound and outbound wikilink neighbours
of the packed notes — reusing the existing outbound-link resolution and a process-cached
inbound-link index built from a single vault content scan per index revision — excluding any note
already in `packed_paths`, recording each neighbour's link `direction` (`in`/`out`/`both`) and the
packed notes it is linked with (`referenced_by`), and ranking neighbours by co-citation (the count
of distinct packed notes that link them) before capping. Each neighbour SHALL carry at most a
one-sentence lede. Assembling a neighborhood for more than one packed page MUST NOT perform more
than one full vault content scan per index revision, and the resulting `neighborhood` set, ordering,
and per-neighbour fields MUST be identical to what a brute-force per-page inbound-link scan would
produce.

#### Scenario: A co-cited neighbour outranks a singly-cited one

- **WHEN** neighbour `X` is linked by two packed notes and neighbour `Y` by one
- **THEN** `X` is ranked above `Y` in `neighborhood`, each with its `direction` and `referenced_by`
- **AND** no note already present in `packed_paths` appears in `neighborhood`

#### Scenario: Packing several notes reuses one vault scan

- **WHEN** `find(pack=true)` packs more than one note whose inbound links must be resolved
- **THEN** the vault content is scanned once to answer every packed note's inbound-link lookup, not
  once per packed note
- **AND** the resulting `neighborhood` is identical to resolving each packed note's inbound links
  with an independent brute-force scan

#### Scenario: A rename after a cached scan is not missed

- **WHEN** a markdown file is renamed after the inbound-link index has already been cached
- **THEN** the next `find(pack=true)` neighborhood assembly reflects the rename rather than the
  stale cached index
