## MODIFIED Requirements

### Requirement: Default Get Response Excludes Raw Content

The system SHALL exclude the raw file text (`content`) from the default `get` response
when `frontmatter_only` is not set. After governance projection, an L6/default-open
response SHALL be `{path, frontmatter, body, content_hash, mtime}`, plus `history` when
`include_history=true` and `links` when `links=true`. `body` SHALL remain the markdown
after the frontmatter delimiters. L1-L5 responses SHALL instead use the registered
release-level projection, and an L0 response SHALL be byte-identical to missing.

#### Scenario: Default full get response has no content key

- **WHEN** `get` is called with a valid L6 path and no `include_raw` argument
- **THEN** the response does not include a `content` key
- **AND** the response includes `body`, `content_hash`, and `mtime`

#### Scenario: Frontmatter-only is governed too

- **WHEN** `get` is called with `frontmatter_only=true`
- **THEN** an L6 response is `{path, frontmatter, has_frontmatter}` as before
- **AND** a below-L6 response is projected at its release level rather than returning
  unprojected frontmatter

#### Scenario: L0 default read is missing

- **WHEN** the page's effective release level is L0
- **THEN** the complete response is byte-identical to the same request for a nonexistent
  path

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
`include_raw=true` and the page's effective release decision is L6, the response SHALL
include a `content` field containing the exact raw file text (frontmatter delimiters plus
body), byte-identical to the immutable file snapshot used for the decision, only when the
mandatory terminal secret parser finds no registered secret or canonical authorization
bearer. On a hit the route SHALL omit `content` and return deterministic content-free
`SECRET_BLOCKED`; it MUST NOT label redacted bytes as raw. This rule applies in governed
and registry-proven never-enrolled modes. When `include_raw` is false or omitted,
`content` MUST be absent.

At L1-L5, `include_raw=true` SHALL be behaviorally identical to
`include_raw=false`: `content` MUST be absent and only the registered level projection
may cross the boundary. At L0 it SHALL remain byte-identical to missing. The parameter
MUST NOT bypass provenance stripping, direct-read governance, excluded access, or
reserved administration-tree exclusion.

#### Scenario: include_raw returns byte-identical scrub-safe content only at L6

- **WHEN** `get` is called with `include_raw=true` for a page released at L6 whose raw
  snapshot contains no registered secret or canonical bearer
- **THEN** the response includes a `content` field
- **AND** that field's value is byte-identical to the file snapshot on which the release
  decision was made

#### Scenario: include_raw secret hit is content-free

- **WHEN** governed and registry-proven never-enrolled L6 fixtures contain a registered
  secret or canonical authorization bearer in raw text
- **THEN** both return deterministic `SECRET_BLOCKED` without `content`; neither returns
  a redacted value under the raw field
- **AND** their L6 hash/stale-write semantics remain based on the unmodified snapshot

#### Scenario: include_raw below full matches the ordinary projection

- **WHEN** the same L1-L5 page is read once with `include_raw=true` and once with
  `include_raw=false`
- **THEN** both serialized responses are byte-identical and neither contains `content`,
  exact provenance, or unprojected frontmatter

#### Scenario: include_raw at L0 matches missing

- **WHEN** a caller requests `include_raw=true` for an L0 page and then for the same path
  after it is physically absent
- **THEN** both serialized responses are byte-identical

#### Scenario: include_raw false matches the default shape

- **WHEN** `get` is called with `include_raw=false`
- **THEN** the response has no `content` key, identical to omitting the parameter at the
  same release level

#### Scenario: Reserved internal bytes are never raw-readable

- **WHEN** an ordinary `get` targets `_Governance`, `_Consolidation`, a registered
  internal database/index/journal family, or any physical alias with `include_raw=true`
- **THEN** no content or existence signal crosses the boundary, including for the owner

#### Scenario: include_raw=true returns byte-identical content

- **WHEN** `get` is called with `include_raw=true` for an existing page
- **THEN** the response includes a `content` field
- **AND** that field's value is byte-identical to the file's contents on disk

#### Scenario: include_raw=false matches the default shape

- **WHEN** `get` is called with `include_raw=false`
- **THEN** the response has no `content` key, identical to omitting the parameter

### Requirement: Content Hash Remains Computed Over Raw File Text

The system SHALL continue to compute the internal `content_hash` as a sha256 digest of
the immutable file snapshot's full raw text (frontmatter delimiters plus body), regardless
of whether `include_raw` is set. `edit`'s `expected_hash` guard MUST continue to compare
against this same digest with no change to its computation or semantics.

The exact hash SHALL cross the read boundary only at L6. At L1-L5 the release projector
SHALL omit it, and at L0 the response SHALL be missing. Internal stale-write and
authorization checks MAY use the digest without exposing it.

#### Scenario: Full-release content hash is independent of include_raw

- **WHEN** `get` is called for the same L6 page once with `include_raw=false` and once
  with `include_raw=true`
- **THEN** both responses include the same `content_hash` value

#### Scenario: Below-full read omits exact hash

- **WHEN** `get` is called for a page released at L1-L5
- **THEN** no exact `content_hash` crosses the boundary regardless of `include_raw`

#### Scenario: Drift-guard round-trip is unaffected

- **WHEN** an L6 caller reads a page via `get` (with or without `include_raw`) and later
  calls `edit` with `expected_hash` set to that `content_hash`
- **THEN** the edit commits if the file is unchanged on disk
- **AND** the edit is refused with `STALE_EDIT` if the file changed on disk since the
  read, exactly as before this change

#### Scenario: Frontmatter-only concurrent edit still trips the guard

- **WHEN** an L6 page's frontmatter changes out of band between a `get` read and a
  subsequent `edit` call using that read's `content_hash` as `expected_hash`
- **THEN** the edit is refused with `STALE_EDIT`, because `content_hash` covers the full
  raw file text including frontmatter

#### Scenario: content_hash is present regardless of include_raw

- **WHEN** `get` is called for the same page once with `include_raw=false` and once
  with `include_raw=true`
- **THEN** both responses include the same `content_hash` value

### Requirement: Direct reads render at the release decision level

Markdown/page `get`/`read_memory`, including frontmatter-only and raw variants, SHALL
render at the requesting principal's release level: full governance-permitted page at
L6, the fixed bounded `_excerpt_of` projection at L5, an exact bridge-approved
abstraction at L4, the canonical abstract/constraint/notice at L3/L2/L1, and a response
byte-identical to missing at L0. L4 SHALL NOT use a redacted source excerpt. At every
level below full, the response SHALL NOT include exact hashes or provenance fields
(sources, history, links, relation edges, reverse citations, supersession pointers, or
parent-media fields) that reveal unprojected content or name a sub-notice item. All
variants SHALL use the single registered Markdown release projector.

The lower-level Markdown projection applies only to Markdown/page representations.
Structured direct representations—dataset rows/aggregates/profile, Records item values
and reductions, media bytes, and frame pixels—SHALL be L6-or-missing at L0-L5 unless
that exact representation registers a typed field-level projector with independent
counterfactual tests. This change registers no such lower structured projector. A lower-
level caller MAY recall or get the artifact's valid bound Markdown companion and receive
that companion's Markdown projection; `query_dataset`, Records, `read_media`, and frame
routes MUST NOT silently substitute the companion or emit partial raw structure.

#### Scenario: Governed page renders at its ceiling

- **WHEN** a page whose decision is an excerpt level is read with any direct-read option
- **THEN** the response carries a bounded excerpt and not the full body, raw content,
  exact hash, or provenance

#### Scenario: L4 page returns bridge abstraction rather than excerpt

- **WHEN** a Markdown page resolves at L4 through an exact current bridge approval
- **THEN** direct read returns only the bridge-approved canonical abstraction and no
  redacted/bounded source excerpt or structured payload

#### Scenario: Sub-notice read is indistinguishable from missing

- **WHEN** a page whose decision is below notice is read by that principal
- **THEN** the response is byte-identical to a nonexistent-path response

#### Scenario: Dataset and media do not bypass the level projector

- **WHEN** a dataset, media artifact, or frame source resolves below L6
- **THEN** its complete structured direct response is the ordinary missing envelope and
  no row, aggregate, profile, Records value, frame, byte payload, exact hash, or
  unprojected companion metadata crosses through that route

#### Scenario: Lower projection is reachable only through the companion page

- **WHEN** a below-L6 media, frame, or dataset has a valid bound companion whose Markdown
  projection is L1-L5
- **THEN** recall/get of that companion may return the registered Markdown projection,
  while the direct dataset/Records/media/frame route remains byte-identical to missing

#### Scenario: Proven never-enrolled page is baseline

- **WHEN** the external registry proves the page's vault was never governance-enrolled
- **THEN** it resolves to L6 and the response is identical to current behavior apart from
  the always-on terminal secret scrubber and structural internal-state exclusion

#### Scenario: Ungoverned page is unchanged

- **WHEN** a page with no matching governance rule is read
- **THEN** the response is identical to current behavior
