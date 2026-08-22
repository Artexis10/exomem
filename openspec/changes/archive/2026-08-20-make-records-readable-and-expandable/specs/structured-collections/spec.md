## ADDED Requirements

### Requirement: Records Markdown items may declare managed readable presentation

A Records Markdown-item manifest MAY declare one closed, versioned, bounded `record_presentation` recipe. This newly reserved namespaced key SHALL NOT reinterpret unrelated legacy `presentation` extension data. Version 1 SHALL support bounded summary descriptors and bounded `table`, `notes`, and `details` sections. Parent references SHALL bind declared item-schema fields. Each table column SHALL declare a unique child field, optional label, exact scalar projection type, and optional link kind only for links.

The table contract SHALL be a derived render/egress allowlist, not another canonical schema. Undeclared child keys remain canonical but SHALL NOT enter managed tables or governed queries. Planning, other storage strategies, unknown keys/versions/kinds/types, duplicate fields, incompatible parent references, or excessive recipes SHALL refuse. YAML frontmatter remains sole canonical structured authority; presentation SHALL never be parsed back or used as a query source.

#### Scenario: Nested item gains a generic readable recipe
- **WHEN** a Records Markdown-item manifest declares summary, typed table projection, notes, and details
- **THEN** validation returns a normalized domain-neutral contract

#### Scenario: Standalone YAML and child files are not introduced
- **WHEN** a collection opts in
- **THEN** each parent remains one ordinary Markdown file without sibling YAML or per-child files

#### Scenario: Open child object requires explicit safe projection
- **WHEN** a legacy array has arbitrary objects but no table projection
- **THEN** its canonical values remain readable while managed rendering and Markdown-item expansion refuse

#### Scenario: Legacy extension key remains opaque
- **WHEN** an existing manifest has custom `presentation` data but no `record_presentation`
- **THEN** it retains prior parsing/mutation behavior and the custom data is not reinterpreted

#### Scenario: Invalid presentation refuses before mutation
- **WHEN** a recipe names an absent parent, scalar table, invalid/duplicate child column, or exceeds bounds
- **THEN** validation refuses content-safely and changes nothing

### Requirement: Managed presentation preserves authored Markdown and exact authority

An opted-in item SHALL contain at most one bounded managed block with exact versioned markers and SHA-256 over canonical JSON of recipe identity plus selected canonical values. Rendering SHALL be deterministic, escaped, inference-free, and labelled generated. Persisted link columns SHALL serialize canonical literals independent of audience policy; whole-file authorization controls access to stored bytes.

Append/value update SHALL render in the guarded audited item batch. Guarded update MAY use `refresh_presentation: true` with empty changes, current item/container guards, and `why`. Reads, query, inspect, revise, rebaseline, and reconcile SHALL NOT render.

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

### Requirement: Inspection validates presentation without trusting it

Inspection SHALL recompute expected bytes from the authorized canonical snapshot without reverse-parsing display. It SHALL return only non-current released items, sorted by item key, with total state counts and truncation metadata. Each entry SHALL contain item key, path, current version, and `missing|stale|malformed|unrenderable`. Type mismatch SHALL be `unrenderable`, MAY name content-safe table/column/child index, SHALL refuse refresh, and requires guarded canonical value update. Presentation-only findings SHALL retain valid lifecycle guards. Mixed-release authorization SHALL precede reading/reflection. Unselected-field edits need not stale the block.

#### Scenario: Hand-edited table cannot override YAML
- **WHEN** displayed value changes without frontmatter
- **THEN** query returns frontmatter and inspect reports mismatch

#### Scenario: Withheld item leaks no presentation fact
- **WHEN** policy withholds an item
- **THEN** inspect returns no item/path/version/marker/row fact for it

#### Scenario: Direct-edit repair remains two audited steps
- **WHEN** direct selected-value edit creates audit gap and stale block
- **THEN** rebaseline acknowledges canonical history without item rewrites and later guarded refresh repairs presentation

#### Scenario: Current items cannot crowd out broken items
- **WHEN** more items exist than the repair cap and only a late item is stale
- **THEN** current entries are omitted, stale is returned deterministically, and counts/truncation are honest

#### Scenario: Type drift has an honest remedy
- **WHEN** a child value no longer satisfies its declared projection type
- **THEN** inspect says `unrenderable`, refresh refuses, and guarded value update is required
