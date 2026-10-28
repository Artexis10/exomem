# Spec Delta

## MODIFIED Requirements

### Requirement: Records inventory is available before a selector is known
Calling `record_memory(action="inspect")` without a collection SHALL return a bounded, governance-filtered inventory of releasable first-class Records manifests and exact Records-layer legacy trackers. Supplying a collection SHALL preserve targeted inspection behavior. Inventory SHALL NOT parse legacy item grammar or return item contents. Inventory SHALL report per-collection coverage counts of committed items and held candidates, and targeted inspection SHALL report coverage with bounded held references, both governance-filtered.

#### Scenario: Empty vault inventory is useful and empty
- **WHEN** a generic client inspects an empty Records layer without a collection selector
- **THEN** it receives empty first-class and legacy inventories plus the route to `describe`

#### Scenario: Denied inventory candidate stays absent
- **WHEN** governance withholds a first-class manifest or legacy tracker
- **THEN** inventory does not reveal its path, title, identity, type, or existence

#### Scenario: Inventory shows a blocked ledger
- **WHEN** one collection holds a refused candidate
- **THEN** inventory reports that collection's committed and held counts without returning candidate contents, and targeted inspection lists the held reference, its timestamp, attempted action and a diagnostics summary

#### Scenario: Store item views are not legacy trackers
- **WHEN** a collection store has published item views under the Records layer, and other Markdown files there carry tracker front matter
- **THEN** collectionless inventory does not authorize or parse the published views as legacy-tracker candidates, and it still reports every other authorized legacy tracker, including one beside the collection's manifest
