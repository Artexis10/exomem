## 1. Relation census and quick fixes (lane G0)

- [x] 1.1 Red `tests/test_relation_census.py`: one snapshot and no Markdown parse; generic
      share, typed and specific coverage; disconnected counts across typed, wikilink and
      `sources:` origins; entity coverage; each structural check's denominator; counts
      mode emits no path, title or vault key; byte-identical output for one generation and
      registry; counts follow the caller's walk filter (twin vaults and a real governed
      policy); unavailable is not zero. Green: `src/exomem/relation_census.py`, the
      `schema_memory` `census` operation with `detail`, the CLI `exomem relations census`
      (managed service over REST first, else a read-only local snapshot), and the
      `relations.census` doctor line. Measured in a fresh process on a synthetic
      3,640-page vault (embeddings off, one WSL2 desktop), through the real snapshot:
      10^5 edges 6.6 s and 10^6 edges 13.1 s, peak RSS 73 MB for both; the snapshot's
      availability proof alone is about 6.7 s of that at this page count. The reduction
      alone, at 10^5 file nodes and 10^6 edges with the proof bypassed: 11.0 s, peak RSS
      132 MB (it was 9.9 s and 702 MB before streaming). The owner under a governed
      policy pays the release filter per page: 4.1 s at 3,600 pages (0.18 s under an
      empty policy; a refused audience 0.008 s), which extrapolates to about 75-110 s at
      10^5 pages, past the 60 s MCP floor. A follow-up could give the owner's view the
      tombstone set directly, since for the owner the filter excludes only tombstones;
      not built here.
- [x] 1.2 Red `tests/test_relation_census_sample.py`: the sample is seeded and stratified by
      family; a judged file folds with a Wilson interval; absent judgements report
      `unmeasured`. Green: `relation_census.sample`, `fold_judgments`, and the CLI
      `--sample`, `--sample-out`, `--seed` and `--judged` flags.
- [x] 1.3 Red in `tests/test_memory_schema.py`:
      `test_infer_save_ignores_unregistered_and_capitalised_observed_labels` and
      `test_infer_save_still_refuses_dropping_a_used_extension`. Green: the infer save's
      observed set holds only canonical keys (and aliases) of registered extensions in use.
- [x] 1.4 `infer`'s census delegates to the snapshot: `test_infer_census_keys_are_unchanged`
      (keys and, on resolved targets, values match the Markdown count).
- [x] 1.5 OpenSpec (this change), the regenerated MCP schema fixture and tool-surface
      contract, and `docs/capabilities.md`.
- [ ] 1.6 After deploy, in the owner's session: run the census on the live vault, record
      its counts and the census-level calibration block for the benchmark in this design.

## 2. Request-time workflows — owned by sibling graph/query engine

- [ ] 2.1 Transfer admission/execution to `add-graph-traversal-queries` G1/G2 and
      collection Q1: author/endpoints/evidence, visible-link resolution, admitted-only
      budgets and typed refusal. Verify those owning gates; do not build `graph_intel/`
      or another evaluator/policy stack. Transfer is not task completion.
- [ ] 2.2 Use sibling G2/G3 hidden/absent twins for request-time paths, counts,
      explanations and continuation, including hidden-only replay debt. Preserve the
      separate census whole-view restriction; no widened census contract is implied.
- [ ] 2.3 Deliver connection paths, evidence chains and reverse-impact review candidates
      through sibling G2/G3/G5 and shared `connect_memory(operation="query")`.
      Carry witness paths into activation through G5/Q8; preserve existing graph-context
      operations and verify the owning MCP/activation gates rather than new tool names.

## 3. The dreamer's graph families (lane G3)

- [ ] 3.1 Reconcile evidence-gap/stale-basis/vocabulary and link proposals with existing
      dreamer link, hydration, alias and convention families. Add only demonstrated
      missing behaviour with its own outcome test; do not add a competing `upkeep_connect`.
      Hubs, communities and structural-authority analytics remain deferred.
- [ ] 3.2 Graph upkeep egress: whole under an empty policy, per item otherwise, and
      `audience_restricted` for the map and topics; coexistence probes; the vault map.

## 4. The benchmark (lane G4)

- [ ] 4.1 Map `graph_reasoning` tasks and edge-quality calibration into sibling G6's
      existing benchmark effort. Record a red baseline and independent answer/path
      oracle, including sparse-edge, unavailable and disclosure negatives; do not
      establish a second benchmark harness.
- [ ] 4.2 Run that shared acceptance after the applicable sibling request-time gates
      and upkeep outcomes. Preserve live census task 1.6 and report its pending status
      separately; no transferred task is complete merely because it has a new owner.
