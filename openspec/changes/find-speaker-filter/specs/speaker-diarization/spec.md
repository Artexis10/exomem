## ADDED Requirements

### Requirement: Speaker-Scoped Search

The system SHALL support filtering `find` results by speaker: a `speakers` filter restricts results
to pages whose `speakers:` frontmatter includes any of the requested names (case-insensitive),
combined with the query and other filters by AND and within the speaker list by OR. When the filter
is unset the system SHALL NOT scope by speaker (default-allow); a page without a `speakers:` list
SHALL NOT match a non-empty speaker filter.

#### Scenario: Find what a speaker said

- **WHEN** `find` is called with `speakers=["Alice"]`
- **THEN** only pages whose `speakers:` frontmatter includes "Alice" (case-insensitive) are returned
- **AND** the filter is AND'd with the query and any other filters

#### Scenario: Unset speaker filter changes nothing

- **WHEN** `find` is called without a `speakers` filter
- **THEN** results are unchanged — no page is hidden on the basis of its speakers
