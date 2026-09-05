## Context

See `proposal.md` for motivation and `specs/referent-resolution/spec.md` for behavior. The pure resolver currently sees categorical hits and edges but cannot inspect vault pages; only the find runtime owns `vault_root`. Graph evidence is derived from a bounded top-ten hit prefix, and hit ordering and serialized hit payloads must remain unchanged. The existing `descriptors` field is intentionally broad because it supports attribute evidence; it is not narrow enough to decide whether promotion needs qualifier grounding.

## Goals / Non-Goals

**Goals:**

- Define `qualifiers` as the deduplicated contiguous run of non-stopword, non-count tokens immediately preceding the cue noun within the same three-token window used for count detection.
- Keep qualifier matching deterministic, categorical, bounded to ten anchors, and available to the pure resolver without I/O.
- Reuse the existing parsed-page cache and the same stem/prefix matching rule used by attribute evidence.
- Preserve the pre-change two-kind promotion expression for empty-qualifier cues and preserve exact-name resolution.

**Non-Goals:**

- Changing ranking, graph traversal, candidate caps, tool parameters, vault state, or the wide `descriptors` input to `_attribute_matches`.
- Treating fuzzy names, retrieval presence, trailing topical words, or topic adjacency alone as qualifier evidence.

## Decisions

### Separate qualifiers from wide descriptors

`descriptors` remains the deduplicated wide residual query-token tuple used by `_attribute_matches`. `qualifiers` is a second, narrower tuple derived only from the contiguous pre-nominal run. A stopword or count token ends that run, and the noun itself is excluded. Words after the noun never enter `qualifiers`, so post-nominal phrasing deliberately falls back to the pre-change two-kind rule.

### Carry parsed anchor tokens on `HitFact`

Add an optional tuple of normalized title/body tokens to `HitFact`. In `resolve_for_find`, hydrate only the top-ten hit prefix, and only when qualifiers and graph traversal are both active, through `find_corpus.CACHE`. Memoize normalized tokens by the parsed page's snapshot hash. The pure resolver pre-stems qualifiers, computes the qualifier-bearing seed set once, and then checks edge seed membership inside the entity loop.

A parallel mapping argument was considered, but it would split facts about one hit across two inputs and make permutation/bounds reasoning harder. Reading pages inside `resolve_referents` was rejected because it would violate the pure-layer contract.

### Prefer qualifier-bearing graph evidence for non-exact candidates

For qualifier cues and non-exact candidates, select the first qualifier-bearing graph edge under the existing deterministic edge order when any exists; otherwise retain the existing first edge for candidate evidence and fail the qualifier gate. Exact-name candidates always retain the pre-change first edge. This lets legitimate graph-only corroboration resolve while keeping distractor evidence visible.

### Gate only the final non-exact promotion

Compose all existing evidence unchanged. When `qualifiers` is empty, apply exactly the pre-change exact-name-or-two-kinds rule. Otherwise require both two independent kinds and qualifier-bearing attribute or graph evidence before promoting a non-exact match. Attribute evidence continues to match the wide `descriptors` tuple plus cue noun. No informational reason counter is added, avoiding release-gate and response changes.

## Risks / Trade-offs

- [Anchor parsing adds synchronous work] → Bound parsing to ten hits, skip it when graph traversal is off, reuse the existing content-aware page cache, memoize tokenization by snapshot hash, and hoist qualifier matching out of entity fan-out.
- [A qualifier appears only in metadata outside title/body] → Deliberately does not qualify; the contract names title/body anchors and attribute evidence separately.
- [Multiple graph anchors disagree] → Prefer a qualifying anchor deterministically without dropping the non-qualifying graph candidate.
- [A qualifier trails the cue noun] → Deliberately use the old two-kind rule; the narrow field prevents arbitrary trailing topic words from becoming a gate.

## Migration Plan

Ship as an additive precision fix with no data migration. Rollback is a code revert; the response schema and stored state do not change.
