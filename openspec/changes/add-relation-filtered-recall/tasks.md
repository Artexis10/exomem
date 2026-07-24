## 1. Stage 1 Red — Sidecar Relation Query

- [ ] 1.1 New `tests/test_epistemic_graph_relation_filter.py`: canonical, alias,
      and parent-roll-up matching; direction semantics (outbound/inbound/any,
      anchor-relative vs candidate-relative, symmetric no-op); anchor join with
      anchor exclusion; placeholder exclusion; block-endpoint resolution to
      owning files; DISTINCT participant paths with deterministic provenance
      tie-break; v6→v7 invalidation and rebuild convergence; cache-token change
      on the schema bump; missing/stale/disabled readiness statuses;
      single-flight background rebuild scheduling.
- [ ] 1.2 Run the new file and confirm red.

## 2. Stage 1 Green — Sidecar Implementation

- [ ] 2.1 `src/exomem/epistemic_graph.py`: bump `SCHEMA_VERSION` to 7; add
      `idx_graph_edges_relation_type(relation_type, src_key, dst_key)` and
      `idx_graph_edges_parent_relation(parent_relation, src_key, dst_key)`;
      implement `RelationFilterResult` and `relation_participants(keys, anchor,
      direction)` through the identity-gated read snapshot using UNIONed
      indexed lookups and `graph_nodes` joins; add single-flight
      `schedule_background_rebuild(vault_root)`.
- [ ] 2.2 Verify the v6→v7 healing path: a stale-identity sidecar must converge
      through a full rebuild; if path refresh does not re-stamp identity, wire
      the stale-marker path to trigger the rebuild.

## 3. Stage 1 Red — Find Integration

- [ ] 3.1 New `tests/test_find_relation_filter.py`: intersection with
      categories/kinds/types/tags/structured filters; empty-query filter-only
      ordering; `relation_of` anchoring; `graph=false` still filters; unknown
      key raises `INVALID_RELATION_FILTER` with suggestions; deprecated key
      advisory; the full degrade matrix (sidecar available/missing/stale/
      disabled × filter present/absent) with warming outcomes never cached;
      freshness key includes the graph token and relation-registry identity in
      keyword and empty-query modes when the filter is active; absent-filter
      byte-identity regression reusing the typed-lane parity harness.
- [ ] 3.2 Run and confirm red.

## 4. Stage 1 Green — Find Implementation

- [ ] 4.1 `src/exomem/find.py`: add the three parameters; canonicalize and
      reject through the relation registry; resolve participants once per
      request and intersect `eligible_paths`; apply the parent-path constraint
      on the unit-level branch after raising warming first; widen
      `_freshness_key` conditions; extend both request cache key tuples; thread
      the `relation_match` annotation through the bundle-to-hit path and the
      hit dataclasses.
- [ ] 4.2 Full stage-1 suite green plus the absent-filter parity regression.

## 5. Stage 2 — Command Surface And Gates

- [ ] 5.1 Command tests: `op_find`/`op_ask_memory` pass-through, `relation_of`
      memory-identifier resolution, error-envelope mapping assertions.
- [ ] 5.2 `src/exomem/commands.py`: add parameters and Google-style docstring
      Args to both ops; add one bounded relation sentence to bootstrap search
      guidance.
- [ ] 5.3 Regenerate `tests/fixtures/mcp_tool_schemas.json` and
      `src/exomem/tool_surface_contract.json` via `scripts/dump-tool-schemas.py`;
      schema-fidelity and tool-surface gates green; record the pending sha in
      `deploy/chatgpt/personal-plugin-contract.json` if the surface hash moves.
- [ ] 5.4 Golden additions in `tests/test_retrieval_golden.py` against the
      authored typed-relation fixtures (available → expected hits;
      sidecar-absent → warming, not empty); add `CEIL_RELATION_FILTER_MS` to
      `tests/test_latency_gate.py` calibrated on the synthetic vault; confirm
      `tests/golden/relation_compatibility.yaml` is untouched.
- [ ] 5.5 Ruff on changed files; full lean pytest suite; latency gate; record
      verification evidence below.
