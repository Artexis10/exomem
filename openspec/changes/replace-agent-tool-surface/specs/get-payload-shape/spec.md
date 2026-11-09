## MODIFIED Requirements

### Requirement: Direct reads honor the excluded access tier

`get`/`read_page` (and the Tier-2 direct-content reads, the `inspect_memory` operations
`dataset.query` and `media.image`, `media.video`) SHALL consult the `_access.yaml` access tier for the requested
path and SHALL refuse any path resolving to the `excluded` tier. The refusal
SHALL be indistinguishable from a genuinely missing path: identical error code,
identical response shape, identical message text, and no echo of the requested
path. No frontmatter, body, dataset row, media frame, or metadata of an excluded
page SHALL cross the boundary through these surfaces.

#### Scenario: Excluded page reads as missing

- **WHEN** `get`/`read_page` is called with a path under an `excluded` subtree
- **THEN** the response is byte-identical in code, shape, and text to the
  response for a nonexistent path, and does not contain the requested path

#### Scenario: Excluded dataset and media are refused

- **WHEN** `dataset.query` targets an excluded data file, or a `media.*` operation targets
  an excluded media artifact
- **THEN** the operation refuses with the missing-path contract and returns no
  rows and no frames

#### Scenario: Indexable content is unaffected

- **WHEN** a `read-write`, `readonly`, or `append-only` path is read
- **THEN** the response is unchanged from current behavior

### Requirement: Direct reads render at the release decision level

Markdown/page `get`/`read_page`, including frontmatter-only and raw variants, SHALL
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
that companion's Markdown projection; `dataset.query`, Records, `media.*`, and frame
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

### Requirement: Long extracted text reads in bounded windows

`read_page` SHALL accept an optional `offset`, a character offset into a Source's extracted text. For a Source released in full whose extracted text is longer than one window, the response SHALL carry the Source's path, title, stable identifier and metadata, and one window of the extracted text from `offset`, 0 by default, as `body`. The window SHALL be at most 32 KiB of characters and SHALL end at a paragraph or line break. The response SHALL carry `window` with `offset`, `next_offset`, `total_chars`, `part` and `parts`. `next_offset` SHALL be greater than `offset`, and null at the end of the text. Offsets SHALL be measured from the current content of the Source page and its parts. So following `next_offset` from 0 to null SHALL return the whole extracted text as it is during the walk, in order, exactly. A window SHALL withhold what the read withholds from the caller. A window SHALL carry `body_truncated: true` and no `content_hash` or `evidence_version`, so it cannot guard a write of the whole page. A read of a part page SHALL return the same response under its Source page's identity, under the access decision of the Source page. An offset that is not a whole number, an offset outside the text, or an offset on a page with no long extracted text SHALL be refused. A page that is not a Source with long extracted text SHALL read as before.

#### Scenario: Walking the windows returns the whole text
- **WHEN** a caller reads a multi-megabyte Source from offset 0 and follows `next_offset` until it is null
- **THEN** the joined windows equal the extracted text exactly

#### Scenario: A walk after an edit reads the text as it is now
- **WHEN** the Source page is edited and a caller then walks its windows
- **THEN** every `next_offset` is past its `offset`
- **AND** the joined windows equal the extracted text as it is after the edit

#### Scenario: A window cannot guard a write
- **WHEN** a caller reads one window of a long Source
- **THEN** the response carries `body_truncated: true` and no `content_hash`

#### Scenario: A part page reads as its Source
- **WHEN** a caller reads the path of a part page
- **THEN** the response carries the Source page's path and stable identifier

### Requirement: Restricted refusals of path-taking doors complete at the direct-read floor

For a restricted caller, as "Restricted direct-read refusals complete at a fixed floor" defines one, every refusal of an `inspect_memory` or `manage_memory` operation that takes a path, and of `dataset.query`, SHALL complete no sooner than the same floor F, under the same rules for surfaces, overruns, owners and successes. Each of these doors SHALL decide that a named path is withheld before work that a missing path would skip, and SHALL then refuse with the missing-path response.

On a governed vault, `list_inbound_links` SHALL give a restricted caller the same empty success for every target that the caller may not see, withheld or missing, whatever pages the caller may see link to it. It SHALL omit the echoed `target` on every outcome for a restricted caller, and SHALL complete each empty answer no sooner than F. A target that the caller may see is not padded.

#### Scenario: A withheld dataset and a missing dataset refuse alike, at the floor

- **WHEN** a restricted caller queries a withheld CSV, a withheld page, a withheld folder, a `//` or `/./` spelling of the withheld CSV, and a missing path
- **THEN** every call refuses with the same missing-path response
- **AND** each refusal completes no sooner than F

#### Scenario: Link suggestions for a withheld page refuse like a missing page

- **WHEN** a restricted caller asks `inspect_memory` for `connections.suggest_links` for a withheld page and for a missing page
- **THEN** both refuse with byte-identical `NOT_FOUND` responses, no sooner than F

#### Scenario: Inbound links of a withheld target match a missing target

- **WHEN** a page that a restricted caller may see links a withheld page and a missing page
- **AND** the caller lists inbound links of each
- **THEN** both responses are byte-identical empty successes and carry no `target`
- **AND** each completes no sooner than F
