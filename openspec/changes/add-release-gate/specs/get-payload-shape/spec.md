# get-payload-shape

## ADDED Requirements

### Requirement: Direct reads render at the release decision level

`get`/`read_memory` SHALL render a governed page at its release decision's level
for the requesting audience: full frontmatter and body only at full disclosure;
a bounded excerpt at excerpt levels; an approved abstraction or constraint at
those levels; and, below notice, a response byte-identical to a missing path. At
any level below full, the response SHALL NOT include provenance fields (sources,
history, relation edges, supersession pointers) that name a sub-notice item.

#### Scenario: Governed page renders at its ceiling

- **WHEN** a page whose decision is an excerpt level is read
- **THEN** the response carries a bounded excerpt and not the full body

#### Scenario: Sub-notice read is indistinguishable from missing

- **WHEN** a page whose decision is below notice is read by that audience
- **THEN** the response is byte-identical to a nonexistent-path response

#### Scenario: Ungoverned page is unchanged

- **WHEN** a page with no matching governance rule is read
- **THEN** the response is identical to current behavior
