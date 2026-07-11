# Proposal: add-typed-graph-find-lane

## Why

The governed typed graph is proven superior to Basic Memory on graph-dependent
tasks (see `docs/comparison-basic-memory-graph.md`), but it is invisible in
ordinary recall: `find()`'s graph fusion lane still expands 1-hop outbound
**wikilinks** only, while the richer typed sidecar (`Knowledge Base/.graph.sqlite`
— typed relations, frontmatter provenance edges, semantic-block edges,
direction, lifecycle state) sits unused at ranking time. The owner has decided
to reverse the earlier "graph must not change `find` ordering" non-goal
(stated in `add-epistemic-graph/design.md`, `add-epistemic-graph/proposal.md`,
`add-governed-relation-registry/tasks.md`, and scaffold `SKILL.md`): the graph
advantage must show up in normal `find`/`ask_memory` answers — the moat has to
be habitual product value, not a separate opt-in surface (improvement #2 in
`docs/comparison-basic-memory-graph.md`, backlog #1 in
`docs/product-gap-matrix.md`).

## What Changes

- The existing `graph` fusion lane in `find_candidates.py` reads the typed
  epistemic-graph sidecar instead of per-page outbound wikilink resolution:
  bidirectional typed-edge expansion from query-relevant seeds, batch SQL,
  relation-family-aware ordering from the governed relation registry,
  unresolved-placeholder targets skipped.
- **Default-on with byte-identical fallback**: when the sidecar is unavailable
  (disabled via `EXOMEM_DISABLE_GRAPH_INDEX`, not yet built, or
  schema/registry-drift-invalidated), the lane falls back to the current
  wikilink expansion, byte-identical to today's ordering.
- Hits that entered the candidate set via graph expansion carry a
  graph-provenance annotation (relation type, direction, seed page) in the
  `find` result envelope — additive field, no existing field changes.
- The typed sidecar's freshness/generation token joins `_freshness_key()` in
  `find.py` so the hot cache never serves rankings computed against a stale
  graph.
- New golden fixture tier with typed relations proves the lane surfaces
  typed-graph neighbours; existing goldens continue to guard the fallback
  path.
- **Deliberate spec reversal** (owner-approved): the living contract surfaces
  that state "`find` ordering is unchanged" are revised — scaffold
  `_Schema/SKILL.md` and affected docs. Historical change artifacts are left
  as history; this change supersedes their non-goal.
- Latency: batch sidecar reads must hold the existing latency gate
  (`tests/test_latency_gate.py`, `CEIL_GRAPH_MS=1000`, `CEIL_TOTAL_MS=5000`).
- No new `RankingConfig` knobs in v1: the lane keeps its `LANE_ORDER` slot,
  existing intent weights (incl. `relationship` 1.8x) and `graph_seed_cap`
  apply unchanged, so the ranking-autotune loop keeps working without schema
  changes to `ranking_config.json`.

## Capabilities

### New Capabilities
- `graph-find-ranking`: typed-graph-backed candidate expansion and
  graph-provenance annotation in `find`'s fusion pipeline, with governed
  fallback semantics and default-on behavior.

### Modified Capabilities
- `live-index-freshness`: the `find` hot-cache freshness key SHALL include the
  typed-graph sidecar's content generation token whenever the graph lane can
  read the sidecar (today it covers walk triples, embedding/CLIP tokens, and
  the lexical backend token only).

## Impact

- Code: `src/exomem/find_candidates.py` (graph lane), `src/exomem/find.py`
  (`_freshness_key`), `src/exomem/epistemic_graph.py` (batch neighbour read
  API + cache token), `src/exomem/find_results.py` / result envelope
  (annotation), scaffold `_Schema/SKILL.md` (contract line), docs.
- Tests: new unit tests for lane expansion + fallback equivalence; new golden
  tier `tests/golden/` (guarded — orchestrator-owned edits);
  `tests/test_latency_gate.py` must stay green unchanged;
  `tests/test_retrieval_golden.py` extended, not weakened.
- Systems: `ask_memory` (MCP) inherits the improved ordering transparently;
  ranking-autotune loop unaffected (no knob schema change);
  `graph_value_benchmark.py` gains a recall-visibility check.
- Explicitly NOT in scope: relation authoring/acceptance queue (separate
  change `add-relation-acceptance-queue`), model-backed suggestions, new
  relation vocabulary.
