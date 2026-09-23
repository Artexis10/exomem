## 1. Relation census and quick fixes (lane G0)

- [x] 1.1 Red `tests/test_relation_census.py`: one snapshot and no Markdown parse; generic
      share, typed and specific coverage; disconnected counts across typed, wikilink and
      `sources:` origins; entity coverage; each structural check's denominator; counts
      mode emits no path, title or vault key; byte-identical output for one generation and
      registry; counts follow the caller's walk filter (twin vaults and a real governed
      policy); unavailable is not zero. Green: `src/exomem/relation_census.py`, the
      `schema_memory` `census` operation with `detail`, the CLI `exomem relations census`
      (managed service over REST first, else a read-only local snapshot), and the
      `relations.census` doctor line.
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

## 2. Admission kernel and request-time algorithms (lane G2)

- [ ] 2.1 Red `tests/test_graph_admitted_kernel.py`: edges need their authoring page and
      both endpoints admitted; placeholders follow their naming edge; the inverse view;
      budgets count admitted elements only; the raw ceiling returns `budget_exhausted`
      without a partial result; unavailable is typed; no direct `graph_edges` SQL in
      `graph_intel/`.
- [ ] 2.2 Red `tests/test_graph_egress_noninterference.py`: outputs are byte-identical
      across vault pairs that differ only in withheld material.
- [ ] 2.3 Connection paths, provenance trace and dependants, per-hit support and stale
      basis, the activation `connection` block, and `graph-context` on the kernel, each red
      first, then the `connect_memory` operations and contract regeneration.

## 3. The dreamer's graph families (lane G3)

- [ ] 3.1 Page-contribution tables, hubs, communities, stored-vector link proposals,
      `upkeep_connect`, evidence gaps and stale basis, and vocabulary upkeep, each red first.
- [ ] 3.2 Graph upkeep egress: whole under an empty policy, per item otherwise, and
      `audience_restricted` for the map and topics; coexistence probes; the vault map.

## 4. The benchmark (lane G4)

- [ ] 4.1 The `graph_reasoning` membench family, the edge-quality renderer, the arms and
      adapter capabilities, scorers per dimension with the egress-leak scorer, and the
      recorded red baseline.
- [ ] 4.2 The pre-registered acceptance amendment and the acceptance run after groups 2
      and 3.
