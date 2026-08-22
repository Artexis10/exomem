## Why

Exomem's canonical `exomem://memory/<uuid>` references correctly survive note moves and
renames, but generic agents can leak those opaque identifiers into conversation even
when the search result already includes a human-readable title and path. Stable machine
identity should remain durable without making users decode UUIDs in normal prose.

## What Changes

- Define a single presentation contract for memory references: agents show a note title
  in user-facing prose, add a path or other short disambiguator only when useful, and do
  not expose the raw canonical UUID reference by default.
- Keep `exomem_id` frontmatter and `exomem://memory/<uuid>` unchanged as internal,
  move-safe identity for tool calls, durable stored state, and explicit debugging.
- Apply the same contract to the generic MCP bootstrap and the installed skill scaffold,
  with regression tests that prevent the two guidance surfaces from drifting.
- Leave search/read response schemas unchanged because hits already provide `title`,
  `path`, and `ref` separately.

## Capabilities

### New Capabilities

- `memory-reference-presentation`: define how agents separate stable machine identity
  from human-readable note citations across bootstrap and skill guidance.

### Modified Capabilities

None.

## Impact

- **Agent contract:** bootstrap operating guidance and the generic installed skill gain
  explicit human-facing citation rules.
- **Documentation:** durable-reference guidance consistently distinguishes presentation
  from resolution.
- **Tests:** contract tests assert that both agent surfaces teach title-first citations
  while preserving canonical refs for machine use.
- **Compatibility:** no command input, search/read response schema, reference grammar,
  frontmatter, index, migration, dependency, model, or runtime behavior changes. The
  bootstrap contract version changes because its guidance changes.
