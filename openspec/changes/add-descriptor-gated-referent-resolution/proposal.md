## Why

Counted referent cues can currently resolve descriptor-less entities from two unrelated evidence kinds, producing a false confident identity where the correct result is partial. The live "two Japanese friends" case demonstrates that the existing two-kind rule needs descriptor grounding without weakening exact-name or descriptorless resolution.

## What Changes

- Require a non-exact candidate to have descriptor-bearing attribute or graph evidence whenever the cue contains descriptors, in addition to the existing two-independent-kinds rule.
- Derive bounded descriptor knowledge for at most the top ten graph anchors from their existing parsed title and body content, then thread that categorical fact into the pure resolver.
- Preserve exact-name behavior, descriptorless cues, hit ordering, the MCP surface, and write paths.
- Add pure, envelope, and synthetic benchmark coverage for the live distractor shape and the legitimate descriptor-bearing graph-anchor shape.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `referent-resolution`: Strengthen non-exact resolution so descriptor-bearing cues require descriptor-grounded evidence.

## Impact

The change affects the pure referent evidence model and the read-only find-envelope composition seam, plus their tests and benchmark fixture. It adds no dependency, model, MCP parameter, tool-schema change, vault mutation, or response float.
