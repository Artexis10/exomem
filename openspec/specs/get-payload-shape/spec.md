# get-payload-shape Specification

## Purpose
TBD - created by archiving change dedupe-get-payload. Update Purpose after archive.
## Requirements
### Requirement: Default Get Response Excludes Raw Content

The system SHALL exclude the raw file text (`content`) from the default `get` response
when `frontmatter_only` is not set. The default response SHALL be
`{path, frontmatter, body, content_hash, mtime}`, plus `history` when
`include_history=true` and `links` when `links=true`. `body` SHALL remain the markdown
after the frontmatter delimiters, unchanged from today.

#### Scenario: Default get response has no content key

- **WHEN** `get` is called with a valid path and no `include_raw` argument
- **THEN** the response does not include a `content` key
- **AND** the response includes `body`, `content_hash`, and `mtime`

#### Scenario: frontmatter_only is unaffected

- **WHEN** `get` is called with `frontmatter_only=true`
- **THEN** the response is `{path, frontmatter, has_frontmatter}` as before
- **AND** this requirement does not change that shape

### Requirement: Raw Content Is Available Opt-In Via include_raw

The system SHALL support an `include_raw: bool = false` parameter on `get`. When
`include_raw=true`, the response SHALL include a `content` field containing the exact
raw file text (frontmatter delimiters plus body), byte-identical to the file's current
contents on disk. When `include_raw` is false or omitted, `content` MUST be absent from
the response.

#### Scenario: include_raw=true returns byte-identical content

- **WHEN** `get` is called with `include_raw=true` for an existing page
- **THEN** the response includes a `content` field
- **AND** that field's value is byte-identical to the file's contents on disk

#### Scenario: include_raw=false matches the default shape

- **WHEN** `get` is called with `include_raw=false`
- **THEN** the response has no `content` key, identical to omitting the parameter

### Requirement: Content Hash Remains Computed Over Raw File Text

The system SHALL continue to compute `content_hash` as a sha256 digest of the file's
full raw text (frontmatter delimiters plus body), computed server-side inside the `get`
read path regardless of whether `include_raw` is set. `edit`'s `expected_hash` guard
MUST continue to compare against this same hash with no change to its computation or
semantics.

#### Scenario: content_hash is present regardless of include_raw

- **WHEN** `get` is called for the same page once with `include_raw=false` and once
  with `include_raw=true`
- **THEN** both responses include the same `content_hash` value

#### Scenario: Drift-guard round-trip is unaffected

- **WHEN** a caller reads a page via `get` (with or without `include_raw`) and later
  calls `edit` with `expected_hash` set to that `content_hash`
- **THEN** the edit commits if the file is unchanged on disk
- **AND** the edit is refused with `STALE_EDIT` if the file changed on disk since the
  read, exactly as before this change

#### Scenario: Frontmatter-only concurrent edit still trips the guard

- **WHEN** a page's frontmatter changes out of band between a `get` read and a
  subsequent `edit` call using that read's `content_hash` as `expected_hash`
- **THEN** the edit is refused with `STALE_EDIT`, because `content_hash` covers the
  full raw file text including frontmatter

