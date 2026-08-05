# get-payload-shape Specification

## Purpose
Cut the token cost of `get` by dropping the redundant raw `content` field from
its default response, since `frontmatter` plus `body` already reconstruct the
file. Raw content stays available opt-in via `include_raw`, and `content_hash`
continues to be computed over the full raw file text regardless of
`include_raw`, so `edit`'s stale-write guard is unaffected.
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

### Requirement: Direct reads honor the excluded access tier

`get`/`read_memory` (and the Tier-2 direct-content reads `query_dataset` and
`read_media`) SHALL consult the `_access.yaml` access tier for the requested
path and SHALL refuse any path resolving to the `excluded` tier. The refusal
SHALL be indistinguishable from a genuinely missing path: identical error code,
identical response shape, identical message text, and no echo of the requested
path. No frontmatter, body, dataset row, media frame, or metadata of an excluded
page SHALL cross the boundary through these surfaces.

#### Scenario: Excluded page reads as missing

- **WHEN** `get`/`read_memory` is called with a path under an `excluded` subtree
- **THEN** the response is byte-identical in code, shape, and text to the
  response for a nonexistent path, and does not contain the requested path

#### Scenario: Excluded dataset and media are refused

- **WHEN** `query_dataset` targets an excluded data file, or `read_media` targets
  an excluded media artifact
- **THEN** the operation refuses with the missing-path contract and returns no
  rows and no frames

#### Scenario: Indexable content is unaffected

- **WHEN** a `read-write`, `readonly`, or `append-only` path is read
- **THEN** the response is unchanged from current behavior

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

