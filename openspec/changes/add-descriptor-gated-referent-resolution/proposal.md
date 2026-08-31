## Why

Counted referent cues can currently resolve qualifier-less entities from two unrelated evidence kinds, producing a false confident identity where the correct result is partial. The live "two Japanese friends" case demonstrates that the existing two-kind rule needs grounding in the entity qualifiers immediately preceding the cue noun without weakening exact-name or unqualified resolution.

## What Changes

- Add `qualifiers`, the deduplicated contiguous run of non-stopword, non-count tokens immediately preceding the cue noun within its three-token window. The existing wide `descriptors` field remains attribute evidence input.
- Require a non-exact candidate to have qualifier-bearing attribute or graph evidence whenever `qualifiers` is non-empty, in addition to the existing two-independent-kinds rule.
- Derive bounded anchor token knowledge for at most the top ten graph anchors from their existing parsed title and body content, then thread that categorical fact into the pure resolver.
- Preserve exact-name behavior, empty-qualifier cues, post-nominal fallback, hit ordering, the MCP surface, and write paths.
- Add pure, envelope, fan-out, and synthetic benchmark coverage for multi-word topical distractors and legitimate qualifier-bearing evidence.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `referent-resolution`: Strengthen non-exact resolution so pre-nominal qualifier cues require qualifier-grounded evidence.

## Impact

The change affects the pure referent evidence model and the read-only find-envelope composition seam, plus their tests and benchmark fixture. It adds no dependency, model, MCP parameter, tool-schema change, vault mutation, or response float.
