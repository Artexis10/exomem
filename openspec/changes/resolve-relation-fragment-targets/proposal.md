# Proposal: resolve-relation-fragment-targets

## Why

A relation is unit-precise on one end and page-only on the other. A
`- relations: kind: [[Target]]` row on a rich `## Heading` unit produces an edge
whose source is that unit's node; `markdown-relations` states this as
"Block-level epistemic precision remains available". The destination has no
equivalent. `epistemic_graph._with_md` strips everything from `#` onward before
the target becomes a file key, so `[[Target#Some Unit]]` silently degrades to
`[[Target]]` and the edge lands page-wide.

The degradation is silent, which is the part that matters. `vault.normalize_wikilink`
documents that it preserves `#anchor` across normalization, and it does — the
fragment survives resolution and is then discarded one call later, with no
warning, no diagnostic, and no authoring feedback. An author who writes the
precise thing gets the imprecise edge and is never told.

The node side is already built. `epistemic_graph` stores `unit_ref` on
`graph_nodes` with its own index, keys a unit node as
`"unit:" + sha256(unit_ref)`, and `_current_unit_status` already resolves a
`parent#fragment` reference to its parent paths and reports `found`, `missing`,
`ambiguous`, or `stale`. What is missing is an edge whose destination uses it.

The concrete consumer is named in a shipped design.
`suggest-epistemic-relations-from-structure` states that `shared_open_question`
"should be revisited as a `duplicates` proposal once relation targets can
address a unit", and its non-goals defer unit-level targets to
"`resolve-relation-fragment-targets`, which owns the schema bump and the
write-path latency evidence." That change was never filed, so the reference
named nothing. This files it.

`_shared_open_question_candidates` already carries `unit_ref` and `anchor` for
both sides in its evidence, precisely so a dismissal expires when the other
page's unit changes. It knows which two units share the question and can only
propose an edge between the two pages.

## What Changes

- A relation target MAY carry a `#fragment`. When it resolves to exactly one
  addressable unit on the target page, the edge's destination is that unit's
  node rather than the page's.
- Every other resolution outcome — no fragment, an unresolvable one, or one
  matching more than one unit — keeps today's page-level edge. Nothing an author
  writes today changes meaning.
- An unresolvable or ambiguous fragment becomes authoring feedback rather than
  silence, alongside the existing malformed-relation and unresolved-target
  counts.
- `shared_open_question` is revisited as a unit-level `duplicates` proposal, the
  consumer this exists for.

## Capabilities

### Modified Capabilities

- `markdown-relations` — unit-level precision applies to a relation's
  destination, not only to its source.

## Impact

- `src/exomem/epistemic_graph.py` — fragment-preserving target resolution and a
  unit-destination edge; `_shared_open_question_candidates`.
- `src/exomem/markdown_relations.py` — the fragment is already accepted by the
  target pattern; the diagnostic for an unresolvable one is new.
- The semantic-authoring contract version, because the relation grammar gains a
  documented target form. That moves the contract digest embedded in the
  scaffold skills and in `tests/fixtures/mcp_tool_schemas.json`, and therefore
  the ChatGPT Personal Plugin fingerprint. `deploy/chatgpt/personal-plugin-contract.json`
  has carried a `pending_tool_surface_sha256` distinct from its registered hash
  since release 0.45.0 (2026-08-11); this change adds to that pending state and
  does not clear it.
- Write-path latency: resolving a fragment costs a unit lookup per relation with
  one. The gate is `tests/test_latency_gate.py`; evidence belongs in this change,
  not in a follow-up.
