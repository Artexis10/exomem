## ADDED Requirements

### Requirement: Structured Filter Eligibility Resolves From Indexes

Every supported structured filter field, including `projects`, `tags`, `types`,
categories, kinds, speakers, file types and the governed unit fields, SHALL be
answerable from a maintained index for the current generation, and eligibility
resolution for a recall SHALL consume that index. A filter plan the index cannot
answer SHALL yield the typed retryable warming outcome and schedule the
single-flight repair; it SHALL NOT evaluate the plan by reading page frontmatter
on the reader thread. Pending-visibility custody SHALL shadow and re-offer the
paths it owns so a filter is evaluated against the committed page, not a stale
index row. The result set of an index-backed evaluation SHALL equal the result
set the full-scan evaluation would produce for the same generation.

#### Scenario: A project filter resolves from the catalogue

- **WHEN** a recall carries `projects=["<key>"]` and the catalogue is live for the current generation
- **THEN** the eligible path set comes from the catalogue without reading any page
- **AND** the set equals the full-scan evaluation of the same filter

#### Scenario: A pending write is filtered against its committed page

- **WHEN** a page's project changes in a governed write whose derived work is still pending
- **THEN** a recall filtered on the new project includes the page and one filtered on the old project excludes it

#### Scenario: A stale index declines rather than walks

- **WHEN** the catalogue generation does not match the live projection for a filtered recall
- **THEN** the eligibility stage returns the retryable warming outcome
- **AND** no page under the scope is read to answer the filter

#### Scenario: An unsupported field is rejected at compile time

- **WHEN** a filter names a field with no index-backed evaluation
- **THEN** compilation fails with an unknown-filter-field error rather than falling back to a scan
