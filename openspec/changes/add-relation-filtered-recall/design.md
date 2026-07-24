# Design: Add Relation-Filtered Recall

## Context

Typed edges live in the derived `.graph.sqlite` sidecar (`graph_edges` with
`relation_type`, `parent_relation`, `registry_status`, `src_key`/`dst_key`,
provenance columns), gated by an identity snapshot combining schema version,
core registry version, and extension registry hash. The find pipeline resolves
all structured filters into one `eligible_paths` set that gates every ranking
lane and the empty-query filter-only path. Indexed category recall (#308)
established the house reliability contract: exact filters never return a
false-empty, never fall back to an unbounded foreground scan, and surface an
explicit typed warming outcome with retry metadata instead.

## Goals / Non-Goals

- Goal: relation filters on `find`/`ask_memory` with the same reliability
  discipline as category filters, composing with every existing filter axis.
- Goal: strictly additive response shape; absent-filter behavior byte-identical.
- Non-goal: changing the relation vocabulary, parser, or golden compatibility
  lock; edge-pair/traversal output (stays in `connect_memory` graph-context);
  per-unit edge anchoring (v1 units qualify through their parent page —
  documented, deferred); matching `raw_relation` on unregistered edges.

## Decisions

1. **API shape: three shortcut-style parameters.** `relations: list[str]`
   (OR within the list), `relation_of: str | None` (anchor page or memory
   identifier, resolved at the command layer; anchor excluded from results),
   `relation_direction: "outbound" | "inbound" | "any"` (default `any`;
   anchor-relative when an anchor is present, candidate-relative otherwise;
   documented no-op for symmetric relations). A page qualifies when it
   participates in at least one typed edge whose canonical `relation_type`
   matches a requested key or whose `parent_relation` matches it — extension
   roll-up is a governance win no free-text `relation:X` search can offer.
   Explicitly NOT a new `unit.*` structured-filter field: the closed `unit.*`
   vocabulary and the lexstore catalog algebra cannot answer edge joins.
2. **Execution on the graph sidecar, not lexstore.** New
   `EpistemicGraphIndex.relation_participants(keys, anchor, direction)` runs
   through the identity-gated read snapshot; endpoints join `graph_nodes` to
   resolve block-level endpoints to owning files and drop placeholders (same
   semantics as `neighbors_for`). Schema bump 6 → 7 adds
   `idx_graph_edges_relation_type(relation_type, src_key, dst_key)` and
   `idx_graph_edges_parent_relation(parent_relation, src_key, dst_key)`;
   matching uses two indexed lookups combined by UNION (an OR would defeat the
   indexes). The bump is additive; a v6 sidecar simply fails the snapshot
   identity check and heals through a full rebuild. Duplicating edges into
   lexstore would need its own materialization, delta repair, and identity
   seam, and would let relation filters answer while the graph index is
   disabled — an incoherent state.
3. **State → outcome mapping.** Current sidecar → authoritative result (an
   empty participant set IS a real "no such edges"). Missing/stale sidecar →
   `RETRIEVAL_INDEX_WARMING` (status `warming`) with bounded retry metadata,
   never cached, plus a single-flight daemon-thread background rebuild
   (mirroring the lexstore background-repair pattern; there is no request-path
   rebuild today, so the warming path introduces the scheduler). Disabled via
   `EXOMEM_DISABLE_GRAPH_INDEX` → `RETRIEVAL_INDEX_WARMING` (status
   `temporarily_unavailable`, reason `graph_index_disabled`), no rebuild
   scheduled. Reuses the existing error code and envelope mapping; no new code
   is minted.
4. **Documents, intersected.** The participant set intersects `eligible_paths`
   immediately after structured-filter resolution, composing AND with
   categories/kinds/types/tags/filters, gating every lane, and making
   empty-query + `relations` a filter-only recall with the documented
   filtered-most-recent ordering. The unit-level branch applies the same set as
   a parent-path constraint after the catalog seed (warming raised first). The
   filter is eligibility, not lane fusion — it works with `graph=false`, and
   the spec pins that.
5. **Unknown keys reject deterministically.** Canonicalize through the relation
   registry (normalize + alias resolution). A key outside the closed governed
   vocabulary can never match a typed edge, so silently returning empty for a
   typo like `implments` would be the worst false-empty; raise a typed
   `INVALID_RELATION_FILTER` naming the key with bounded nearest-canonical
   suggestions. Deprecated keys match with an advisory finding and replacement
   hint. This deliberately diverges from graph-context's silent allowlist
   narrowing; the spec records the divergence.
6. **Freshness and caching.** When the filter is active, the find freshness key
   incorporates the graph sidecar cache token and the relation-registry
   identity in EVERY mode (including keyword and empty-query, which today skip
   the graph token); request cache keys extend with the three new parameters.
   Warming outcomes bypass both caches by construction (exceptions are never
   cached).
7. **Annotation.** Relation-qualified hits carry an additive `relation_match`
   annotation (relation type, direction, counterpart path, matched-via) riding
   the existing bundle→hit mechanism — deliberately distinct from
   `graph_provenance`, which means "entered candidates via graph-lane
   expansion". No existing envelope field changes shape.

## Risks / Trade-offs

- v6 → v7 healing depends on the stale-identity path triggering a full rebuild;
  if path refresh does not re-stamp identity, wire the stale-marker path to
  rebuild — called out as an explicit implementation verification task.
- Broad relations (`links_to`) yield large participant sets; acceptable (an
  in-memory path set, same order as category eligibility) and covered by a
  dedicated latency-gate ceiling (`CEIL_RELATION_FILTER_MS`, calibrated on the
  synthetic vault) rather than raising existing ceilings.
- Relation evidence is KB-tree-scoped (the rebuild walks the KB, like the
  vector sidecar); documented limitation.
- Many existing tests set `EXOMEM_DISABLE_GRAPH_INDEX`; new tests manage it
  explicitly to avoid cross-contamination.

## Sequencing

Two sequential implementation stages inside one lane: (1) sidecar schema +
participant query + find integration with its tests; (2) command surface,
schema-fixture regeneration, golden and latency-gate additions. The MCP schema
fixture is byte-gated on final public signatures, so stage 2 starts only after
stage 1's API settles.
