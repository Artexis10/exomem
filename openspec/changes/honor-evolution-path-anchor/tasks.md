# Tasks

## 1. Product
- [x] 1.1 Red-first test proving `review_memory --mode evolution --path <p>`
      currently ignores `path` (anchor varies with find order), then the
      dispatch fix in `op_review_memory`: path present →
      `evolution_for_path`; path missing → topic route unchanged;
      unresolvable path → explicit error (no silent topic fallback)
- [x] 1.2 Anchor semantics documented in the op docstring / tool surface
      (topic route: surfacing hit; path route: requested page; `chain_id`
      always the active head)

## 2. Benchmark follow-up (after 1.x lands and the bench-cleanup lane merges)
- [x] 2.1 `journeys.py` envelope doc corrected ("oldest path" → "requested
      path"); `test_j1_longitudinal_evolution_green` green via the product
      contract, assertion unchanged (`topic_anchor == v1_path` under
      `--path v1`)
- [ ] 2.2 Ledger 4b.28 closed citing this change, with the corrected
      mechanism (get_page never head-followed; #375 tie-break flip, #378
      innocent) recorded for the file

## 3. Validation
- [x] 3.1 `openspec validate honor-evolution-path-anchor --strict`
- [ ] 3.2 `uv run python -m pytest -q tests/test_evolution.py` plus the new
      commands-level test green; full lean suite no new failures
