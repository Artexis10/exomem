# Add Relation-Filtered Recall

## Why

Typed relations are already governed (25 core types plus parented namespaced
extensions), materialized in the derived graph sidecar, ranked by the typed find
lane, and traversable through graph-context — but the primary recall surface
cannot filter by them. An agent can ask "recall units in category constraint"
with indexed reliability, yet cannot ask "recall what `contradicts` this
decision" or "everything `supersedes`-linked" without dropping to graph-context
traversal. Competing systems expose `relation:X` search with no governance, no
direction, and no reliability contract; closing this gap with the same
never-false-empty discipline as indexed category recall makes relation filters a
first-class, trustworthy recall axis.

## What Changes

- Add `relations`, `relation_of`, and `relation_direction` parameters to `find`
  and `ask_memory`: select KB pages participating in typed edges of the
  requested canonical types (with extension roll-up through `parent_relation`),
  optionally anchored to one page and constrained by direction.
- Execute the filter against the typed graph sidecar through the existing
  identity-gated read snapshot, with two new relation-type indexes and a sidecar
  schema bump; compose the participant set into the existing eligible-paths
  filter seam so it intersects categories, kinds, types, tags, structured
  filters, and empty-query recall.
- Map sidecar states onto the established exact-recall outcome vocabulary:
  authoritative results only from a current sidecar; missing/stale returns a
  typed non-cacheable warming outcome and schedules a single-flight background
  rebuild; disabled returns an explicit temporarily-unavailable outcome. Never a
  silent empty.
- Reject unknown relation keys deterministically with suggestions; accept
  aliases; accept deprecated keys with an advisory finding.
- Annotate relation-qualified hits additively (`relation_match`), distinct from
  graph-lane provenance; responses without the new parameters stay
  byte-identical in every sidecar state.

## Dependency Note

The `graph-find-ranking` capability base lives in the unarchived change
`add-typed-graph-find-lane`, and the outcome vocabulary this change mirrors
lives in the unarchived `restore-indexed-category-recall`
(`category-retrieval-reliability`); `openspec/specs/` has no consolidated base
for either yet. Deltas here are authored as additive requirements against those
changes' spec text and should be reconciled when they are archived/synced.

## Capabilities

### New Capabilities

- `relation-filtered-recall`: Participant-selection semantics, canonicalization
  and rejection rules, reliability/warming contract, composition with other
  filters, and bounded indexed execution for relation filters on the primary
  recall surface.

### Modified Capabilities

- `graph-find-ranking`: Additive requirements — absent-filter byte-identity
  across sidecar states, sidecar schema-bump fallback parity, and the
  `relation_match` annotation's separation from graph-provenance.
- `command-surface`: `find`/`ask_memory` expose the three relation parameters
  with docstring-derived schemas across MCP, REST, CLI, and OpenAPI; bootstrap
  search guidance mentions relation filtering.

## Impact

Expected implementation areas: the epistemic graph sidecar (schema version,
relation indexes, participant query, single-flight rebuild scheduling), find
orchestration (eligibility intersection, freshness keys, request cache keys,
annotations), command surface and generated schema fixtures, golden retrieval
additions, and a new latency-gate ceiling. Existing responses without the new
parameters are unchanged; the relation vocabulary itself does not change
(`tests/golden/relation_compatibility.yaml` stays untouched).
