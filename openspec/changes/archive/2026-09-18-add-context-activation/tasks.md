# Tasks: add-context-activation

Red-first throughout: pure-logic unit tests before wiring; torch/model paths behind
soft-fail seams; every task names its gate.

## 1. Activation index (src/exomem/working_set_index.py)

- [x] 1.1 Red: `tests/test_working_set_index.py` — build from a fixture vault with
      entities (+aliases), hubs, `Products/`/`Systems/` pages, a Planning collection,
      a Records manifest with `claims`, and project keys; assert anchor rows, kinds,
      aliases, categories, links and structural signatures; assert rebuild == incremental
      after writes; assert generation bumps on upsert/delete; assert schema mismatch
      wipes and rebuilds; assert nothing is built under `EXOMEM_DISABLE_WORKING_SET`.
- [x] 1.2 Implement the sidecar (`.working-set.sqlite`, meta row, generation token,
      FTS5 terms, optional vectors via the embedding backend, copy-on-write cache),
      the source walkers (entity registry snapshot, hubs/products/systems pages,
      `planning.query`, `structured_collections` manifests + claims, project keys) and
      incremental update on the recall freshness checkpoint.
- [x] 1.3 Red: recall isolation — `ask_memory` fixtures byte-identical with and without
      the index present (reuse the referents identity test shape).

## 2. Anchor resolution (src/exomem/working_set_resolve.py)

- [x] 2.1 Red: `tests/test_working_set_resolve.py` — evidence kinds per source; the
      two-kinds rule; `partial` with competitors; `ambiguous` on disjoint
      neighbourhoods; negative twins → `unresolved`; `usage_prior` alone never resolves;
      `claims_match` delegates to `collection_claims.route`; `graph_corroboration`
      counted when both candidates are in the primary set; no float leaves the module.
- [x] 2.2 Implement turn analysis (NFKC/casefold tokens, n-grams, cue lexicon, one
      memoised query vector), candidate generation over the index, evidence assembly
      and status derivation; pin band thresholds in `ranking_config`.

## 3. Context roles (src/exomem/context_roles.py, context-roles.yaml)

- [x] 3.1 Red: `tests/test_context_roles.py` — shipped registry loads; override may
      add/narrow but not remove; broken override falls back with a warning; selection is
      deterministic and bounded (≤6) with attribution; `roles_hash` changes with the
      registry; scaffold and plugin copies byte-identical; no-leak gate passes.
- [x] 3.2 Author `src/exomem/_scaffold/_Schema/context-roles.yaml` (14 roles, lanes,
      category sets, anchor-kind defaults, cue patterns) and the plugin copy; implement
      the loader and selector.

## 4. Lanes, current state, packet (src/exomem/working_set.py, working_set_state.py)

- [x] 4.1 Red: `tests/test_working_set_packet.py` — packet shape; per-role caps;
      budget never exceeded and overflow becomes pointers; units before pages;
      superseded units marked or replaced by successors; `current_state` from Records
      first with `source` and `as_of`; abstained packet has empty units/pointers and
      `used_chars = 0`; `due_state` not duplicated; graph expansion depth ≤2 from
      resolved and 1 from partial anchors.
- [x] 4.2 Implement lanes over `_find_semantic_units` (category pushdown, neighbourhood
      paths), `record_governance.query_collection`, `planning.query`, entity facets,
      `epistemic_graph.graph_context`, evidence pointers; dedup and lifecycle marking;
      the current-state resolver; budget enforcement.

## 5. Governance, runtime, surface (egress.py, working_set_runtime.py, commands.py, CLI, REST)

- [x] 5.1 Red: `tests/test_working_set_egress.py` — reuse the referents withheld-page
      fixture: no field of the packet names a withheld page; dependent evidence is
      dropped; `purpose` never enters the cache key.
- [x] 5.2 Implement `guard_working_set` beside `guard_referents`, `working_set_runtime`
      (managed single-flight background index build with `index_warming` abstention;
      unmanaged inline build within budget; soft-fail), `op_activate_context` (release
      gate first, small-limit `find()` for the `retrieval` kind, `_with_due_state`),
      the registry row, `exomem activate` subcommand and the REST route.
- [x] 5.3 Red: `tests/test_activate_context_surface.py` — three-door parity on one
      fixture turn; kill switch → `abstained` with reason `disabled`, tool still on the
      surface; timings spans registered and `sum(stages) <= total_ms` from a real call.
- [x] 5.4 Latency gate: add `CEIL_WORKING_SET_MS` and the 2k→8k scaling bound to
      `tests/test_latency_gate.py` using the model-free synthetic corpus.

## 6. Derived artifacts and docs

- [x] 6.1 Regenerate `tests/fixtures/mcp_tool_schemas.json` (`scripts/dump-tool-schemas.py`),
      both surface digests, the hosted plugin/candidate trees, `docs/capabilities.md`,
      the README tool table; update SKILL.md recall loop (scaffold + plugin) with the
      balanced/maximal activation line; run the public-artifact privacy gate.
- [x] 6.2 `openspec validate --all --strict`; scoped suites green; full sharded corpus
      at the delivery boundary; PR with verification evidence.

## 7. Deferred to follow-up changes (recorded, not done here)

- Host hook injection, continuity token, `anchor=` override, hosted carrier line →
  `activate-context-on-host-turns`.
- `working_set` review family for accept/reject signal → same host change.
- Hot profile → `add-hot-profile`.
