## Context

See `proposal.md` for motivation and `specs/referent-resolution/spec.md` for behavior. The pure resolver currently sees categorical hits and edges but cannot inspect vault pages; only the find runtime owns `vault_root`. Graph evidence is derived from a bounded top-ten hit prefix, and hit ordering and serialized hit payloads must remain unchanged.

## Goals / Non-Goals

**Goals:**

- Keep descriptor qualification deterministic, categorical, bounded to ten anchors, and available to the pure resolver without I/O.
- Reuse the existing parsed-page cache and the same stem/prefix matching rule used by attribute evidence.
- Preserve byte-identical behavior for descriptorless cues and exact-name resolution.

**Non-Goals:**

- Changing cue detection, ranking, graph traversal, candidate caps, tool parameters, or vault state.
- Treating fuzzy names, retrieval presence, or topic adjacency alone as descriptor evidence.

## Decisions

### Carry parsed anchor tokens on `HitFact`

Add an optional tuple of normalized title/body tokens to `HitFact`. In `resolve_for_find`, hydrate only the active top-ten hit prefix through `find_corpus.CACHE`, which reuses the corpus parser and its content-aware cache. The pure resolver compares cue descriptors with those tokens through `_matches_attribute`.

A parallel mapping argument was considered, but it would split facts about one hit across two inputs and make permutation/bounds reasoning harder. Reading pages inside `resolve_referents` was rejected because it would violate the pure-layer contract.

### Prefer descriptor-bearing graph evidence when available

For descriptor cues, select the first descriptor-bearing graph edge under the existing deterministic edge order when any exists; otherwise retain the existing first edge for candidate evidence and fail the descriptor gate. This lets legitimate graph-only corroboration resolve while keeping distractor evidence visible.

### Gate only the final non-exact promotion

Compose all existing evidence unchanged, then require both two independent kinds and descriptor-bearing attribute or graph evidence before promoting a non-exact match. Exact-name promotion and cues with no descriptors retain their existing branches. No informational reason counter is added, avoiding release-gate and response changes.

## Risks / Trade-offs

- [Anchor parsing adds synchronous work] → Bound parsing to ten hits and use the existing content-aware page cache.
- [A descriptor appears only in metadata outside title/body] → Deliberately does not qualify; the contract names title/body anchors and attribute evidence separately.
- [Multiple graph anchors disagree] → Prefer a qualifying anchor deterministically without dropping the non-qualifying graph candidate.

## Migration Plan

Ship as an additive precision fix with no data migration. Rollback is a code revert; the response schema and stored state do not change.
