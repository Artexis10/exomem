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

### Requirement: Restricted direct-read refusals complete at a fixed floor

A restricted caller is a caller from whom the release plane could withhold anything, RAW included: `egress.caller_restricted` is true for it. This covers non-owner audiences, CF-Access scopes, and owners under a connector ceiling. For a restricted caller, every refusal of a direct read SHALL complete no sooner than the repository floor F after the dispatcher received the request. The direct reads are `read_memory` and its `get` and `fetch` routes, by path, by memory reference and by unit reference. F SHALL be one repository constant of 55 ms with no environment or caller override, and a change to F SHALL be a change to this requirement. Behind the MCP and REST surfaces, the surface SHALL hold the response, an error or a result, until F has passed, after its own handling of the refusal, its logging included. That wait SHALL run on the event loop and SHALL hold a worker thread for at most its last 3 ms.

The floor SHALL apply to refusals only. A successful read, an L1 to L4 notice included, SHALL return as soon as it is ready. The refusal of a caller that is not restricted SHALL return as soon as it is ready. The dispatcher SHALL ask whether the caller is restricted only after the read refused. A read by path SHALL ask it once before it renders the page, and for an owner in file mode that question SHALL load no policy.

A refusal whose own work runs past F SHALL complete late with its original refusal. The system SHALL NOT refuse, change or replace a direct read because of its completion time. The system SHALL record each padded refusal, its work time and whether it overran F, in an owner-only call span that carries no path, title or reference. The MCP surface SHALL log a refusal that carries a stable code as one line without its traceback, and SHALL keep the traceback of an exception without a code.

For a restricted caller, a read by path SHALL decide whether the page is withheld entirely before the optional work that grows with the vault or with the page's history: links, history, the query log and the heat signal. That early refusal SHALL use the same release decision as the release gate, SHALL return the same missing-path response, and SHALL leave the same disclosure receipt. An owner without a connector ceiling SHALL keep the existing order.

The work of a refusal SHALL NOT grow with the number of files in the vault, except for one directory check per directory, the first refusal after a file was added, removed or renamed, and the first withheld refusal in a process, which builds the memory-reference index. That refusal MAY complete after F.

This requirement covers pages up to the governed capacity of 1 MiB per item. The refusal of a larger page MAY complete after F.

#### Scenario: A withheld path and a missing path refuse alike, at the floor

- **WHEN** a restricted caller reads a withheld page by path, and then reads the same path after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A hidden-only reference and an absent reference refuse alike, at the floor

- **WHEN** a restricted caller reads a memory reference that only a withheld page holds, and then reads the same reference after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A unit read of a withheld page refuses at the floor

- **WHEN** a restricted caller reads a unit reference on a withheld page, and then reads the same unit reference after the page is deleted
- **THEN** both reads refuse with byte-identical responses
- **AND** each refusal completes no sooner than F after the dispatcher received it

#### Scenario: A notice returns without the floor

- **WHEN** a restricted caller reads a page that the release decision serves at L1 to L4
- **THEN** the read returns the notice, not a missing-page refusal
- **AND** the read returns as soon as it is ready

#### Scenario: An owner refusal is not padded

- **WHEN** an owner without a connector ceiling reads a missing path
- **THEN** the refusal returns as soon as it is ready

#### Scenario: An overrun completes late and is recorded for the owner

- **WHEN** the work of a restricted caller's refusal runs past F
- **THEN** the read completes with its original refusal, after F, and is not refused because of time
- **AND** the owner-only call span records the overrun with no path, title or reference

#### Scenario: A withheld page refuses before the optional work

- **WHEN** a restricted caller reads a withheld page by path with links and history
- **THEN** the read refuses before it reads the edit log or the inbound links, and writes no query-log row for the page
- **AND** the disclosure receipt equals the receipt that the release gate records for that page

#### Scenario: Through the MCP surface, a withheld path and a missing path take the same time

- **WHEN** a restricted caller reads a withheld page by path through the MCP surface, and then reads a missing path in the same folder, in turn several times
- **THEN** the two refusals read alike, apart from the path that each names
- **AND** each completes no sooner than F after the dispatcher received it, and their median completion times do not differ beyond timing noise

#### Scenario: A file added after an earlier refusal is still withheld from error text

- **WHEN** an error for a restricted caller names a withheld page that was created after an earlier error
- **THEN** the error text replaces the reference to that page

### Requirement: Restricted refusals of path-taking doors complete at the direct-read floor

For a restricted caller, as "Restricted direct-read refusals complete at a fixed floor" defines one, every refusal of `browse_memory`, `compile_source`, `connect_memory` and `query_dataset` SHALL complete no sooner than the same floor F, under the same rules for surfaces, overruns, owners and successes. Each of these doors SHALL decide that a named path is withheld before work that a missing path would skip, and SHALL then refuse with the missing-path response.

On a governed vault, `list_inbound_links` SHALL give a restricted caller the same empty success for every target that the caller may not see, withheld or missing, whatever pages the caller may see link to it. It SHALL omit the echoed `target` on every outcome for a restricted caller, and SHALL complete each empty answer no sooner than F. A target that the caller may see is not padded.

#### Scenario: A withheld dataset and a missing dataset refuse alike, at the floor

- **WHEN** a restricted caller queries a withheld CSV, a withheld page, a withheld folder, a `//` or `/./` spelling of the withheld CSV, and a missing path
- **THEN** every call refuses with the same missing-path response
- **AND** each refusal completes no sooner than F

#### Scenario: Link suggestions for a withheld page refuse like a missing page

- **WHEN** a restricted caller asks `connect_memory` `suggest-links` for a withheld page and for a missing page
- **THEN** both refuse with byte-identical `NOT_FOUND` responses, no sooner than F

#### Scenario: Inbound links of a withheld target match a missing target

- **WHEN** a page that a restricted caller may see links a withheld page and a missing page
- **AND** the caller lists inbound links of each
- **THEN** both responses are byte-identical empty successes and carry no `target`
- **AND** each completes no sooner than F
