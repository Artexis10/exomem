## ADDED Requirements

### Requirement: Restricted refusals of path-taking doors complete at the direct-read floor

For a restricted caller, as "Restricted direct-read refusals complete at a fixed floor" defines one, every refusal of `browse_memory`, `compile_source`, `connect_memory` and `query_dataset` SHALL complete no sooner than the same floor F, under the same rules for surfaces, overruns, owners and successes. Each of these doors SHALL decide that a named path is withheld before work that a missing path would skip, and SHALL then refuse with the missing-path response.

`list_inbound_links` answers a missing target with an empty success. For a restricted caller it SHALL answer a withheld target with the same bytes, SHALL omit the echoed `target` on every outcome, and SHALL do the same visibility work for both.

#### Scenario: A withheld dataset and a missing dataset refuse alike, at the floor

- **WHEN** a restricted caller queries a withheld CSV, a withheld page, a withheld folder, a `//` or `/./` spelling of the withheld CSV, and a missing path
- **THEN** every call refuses with the same missing-path response
- **AND** each refusal completes no sooner than F

#### Scenario: Link suggestions for a withheld page refuse like a missing page

- **WHEN** a restricted caller asks `connect_memory` `suggest-links` for a withheld page and for a missing page
- **THEN** both refuse with byte-identical `NOT_FOUND` responses, no sooner than F

#### Scenario: Inbound links of a withheld target match a missing target

- **WHEN** a restricted caller lists inbound links of a withheld page and of a missing page
- **THEN** both responses are byte-identical and carry no `target`
