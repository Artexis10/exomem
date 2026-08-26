## MODIFIED Requirements

### Requirement: Managed presentation preserves authored Markdown and exact authority

An item opted in through valid `record_presentation` or `item_presentation` SHALL contain at most one bounded managed block with exact versioned markers and SHA-256 over canonical JSON of recipe identity plus selected canonical values. Rendering SHALL be deterministic, escaped, inference-free, and labelled generated. Persisted link columns SHALL serialize canonical literals independent of audience policy; whole-file authorization controls access to stored bytes.

Append/value update SHALL render in the guarded audited item batch. Guarded Records update MAY use `refresh_presentation: true` with empty changes, current item/container guards, and `why`. Reads, query, inspect, rebaseline, and reconcile SHALL NOT render. A guarded manifest revision that removes or replaces a presentation recipe SHALL transactionally remove or replace every owned block or refuse. A guarded `maintain_memory(mode="structured-files")` apply SHALL render only the exact blocks in its current preview plan.

Rendering SHALL source-splice exact bytes and preserve BOM, CRLF/LF, body separator/leading blanks, marker-like ordinary prose, and final-newline state. Duplicate/nested/malformed/oversized exact markers SHALL refuse every rendering mutation.

Semantic append replay SHALL hash canonical values plus exact authored body with one valid managed block removed. Renderer bytes/version SHALL not change semantic request identity; malformed/tampered blocks SHALL not qualify. Receipts/item versions still bind exact complete bytes.

#### Scenario: New nested Record is readable
- **WHEN** an opted-in item is appended with nested values and prose
- **THEN** it contains unchanged frontmatter, deterministic readable sections, and byte-preserved prose

#### Scenario: Existing item is backfilled audibly
- **WHEN** a missing block is refreshed with current guards and reason
- **THEN** canonical values remain equal and one audit receipt binds exact new hashes

#### Scenario: Direct YAML edit remains authoritative
- **WHEN** a human changes a selected canonical value without rendering
- **THEN** reads/query use YAML, inspect reports stale, and no read rewrites it

#### Scenario: Ambiguous markers are never guessed
- **WHEN** exact markers are duplicate, nested, malformed, or oversized
- **THEN** inspect reports malformed and rendering refuses

#### Scenario: Exact append replay ignores only valid renderer bytes
- **WHEN** committed append retries identical values/authored body after rendering
- **THEN** it replays without conflict; malformed/tampered markers receive no replay treatment

#### Scenario: Legacy byte shapes survive rendering
- **WHEN** rendering targets BOM, CRLF, blank-body, leading-blank, marker-like-prose, or no-final-newline vectors
- **THEN** exact bytes outside the managed span preserve those properties

#### Scenario: Policy does not rewrite stored presentation
- **WHEN** link release policy changes without canonical byte changes
- **THEN** expected managed bytes/item hash remain unchanged while query egress follows current policy

#### Scenario: Recipe removal cleans every owned block atomically
- **WHEN** a guarded complete manifest revision removes a presentation recipe from a collection whose items contain valid owned blocks
- **THEN** the manifest and exact block removals publish together or no file changes

#### Scenario: Migration renders only the approved plan
- **WHEN** structured-files apply carries the current plan identity and unchanged source snapshot
- **THEN** it writes only the previewed presentation transformations under one terminal receipt

## ADDED Requirements

### Requirement: Structured manifests may declare shared item representation recipes

A versioned Markdown-item collection manifest SHALL accept optional `item_filename` and `item_presentation` recipes defined by the human-owned structured-files capability. Manifest validation SHALL eagerly validate recipe versions, field existence, field eligibility, renderer compatibility, path safety, managed-marker uniqueness, and worst-case bounded output without reading items outside the declared collection source.

#### Scenario: Invalid recipe fails before collection publication

- **WHEN** create or revision validation receives a recipe with an unknown version, absent field, mutable filename field, unsafe path projection, or unbounded presentation
- **THEN** validation refuses with exact recipe diagnostics and writes no manifest, item, template, or audit file

#### Scenario: Recipe is optional and path-independent

- **WHEN** a compatible existing collection declares neither shared recipe
- **THEN** it remains valid and retains its current paths and body behaviour until explicitly revised

### Requirement: Presentation ownership survives recipe removal and conversion

The substrate SHALL recognize managed presentation markers independently of the currently active recipe. Removing or replacing a recipe SHALL either transactionally remove or replace every owned managed block as part of the guarded manifest revision, or SHALL refuse the revision. It SHALL NOT publish a manifest state that leaves a managed block with no active owner.

#### Scenario: Recipe removal cannot orphan generated blocks

- **WHEN** a complete manifest revision removes its presentation recipe while current items contain owned blocks
- **THEN** the revision either includes their exact transactional cleanup or refuses before changing the manifest

#### Scenario: Conversion preserves authored Markdown

- **WHEN** a collection converts from a compatible profile-specific recipe to `item_presentation`
- **THEN** each old owned block is replaced by the new deterministic block in the same batch and Markdown outside the markers remains byte-identical

### Requirement: Inspection covers filenames and all managed presentation state

Targeted collection inspection SHALL scan canonical item paths and recognized managed markers even when the active manifest declares no representation recipe. It SHALL report bounded diagnostics for filename drift, projected path collisions, missing presentation, stale recipe digest, stale item version, orphan managed block, unrenderable selected value, unresolved relationship presentation, and authored changes inside managed authority. Inspection SHALL remain report-only.

#### Scenario: Orphan block is unhealthy without an active recipe

- **WHEN** an item contains a recognized managed block but its manifest declares no owning recipe
- **THEN** inspection reports `orphan_presentation` rather than declaring the collection healthy

#### Scenario: Healthy collection proves representation agreement

- **WHEN** manifest, items, audit, filenames, managed markers, and rendered content agree
- **THEN** inspection returns no representation diagnostics without rewriting any file

### Requirement: Representation transformations use current canonical collection mechanics

Preview and apply SHALL reuse collection discovery, profile validation, source snapshots, same-vault writer serialization, idempotency, audit publication, stable reference resolution, and governance projection. They SHALL NOT create a second item index or treat rendered Markdown as canonical input.

#### Scenario: Manual canonical edit appears in the next plan

- **WHEN** a human validly edits an item between two read-only representation previews
- **THEN** the second plan derives from the new canonical hash and receives a different source snapshot or plan identity

#### Scenario: Apply shares existing writer protection

- **WHEN** representation apply races another mutation in the same vault
- **THEN** the existing writer boundary serializes them and prevents torn file moves or presentation bytes
