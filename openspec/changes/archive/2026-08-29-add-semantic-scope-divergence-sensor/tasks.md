# Tasks — add-semantic-scope-divergence-sensor

## 1. Red-first fixtures

- [x] 1.1 Synthetic-geometry seam: a test helper that seeds `semantic_unit_vectors`
      for a fixture page with constructed vectors (inject via the embedding-index
      seam, no real embedder). Prove the seam by reading the rows back through the new
      `all_semantic_unit_vectors()` accessor (task 2.0).
- [x] 1.2 The 2026-08-29 acceptance fixture: shared-vocabulary page whose units split
      into two vector groups → RED on main (no advisory), verbatim failing output
      recorded before any implementation.
- [x] 1.3 Synonym-swap fixture: same geometry, substituted vocabulary → asserts the
      advisory survives (f20's behaviour-not-vocabulary property).
- [x] 1.4 Twin fixtures: bounded-scope, tangent (below mass), declared hub,
      heterogeneous log — zero advisories asserted positively.

## 2. Sensor

- [x] 2.0 `EmbeddingIndex.all_semantic_unit_vectors()`: one paginated corpus-level
      SELECT of `(parent_path, unit_ref, source_order, vector)` from
      `semantic_unit_vectors`, grouped by `parent_path`; read-only, no schema
      change, no cache coupling to the chunk-keyed `_EmbCache`; unit-tested over
      seeded rows.
- [x] 2.1 Deterministic grouping over `EmbeddingIndex.all_semantic_unit_vectors()`
      rows for one page:
      greedy agglomerative, fixed thresholds, stable tie-breaks, `MAX_JUDGED_UNITS`
      cap with a named skip note. Gates: cohesion φ, separation θ, mass
      (`CLUSTER_MIN_UNITS`), retained scope (`MIN_RETAINED_UNITS`), breadth exclusion
      (v1 rule reused). Constants marked PROVISIONAL with the calibration posture.
- [x] 2.2 Extractive labels via v1 `_terms`; reason code `semantic_cluster_diverges`
      in the v1-compatible advisory shape.
- [x] 2.3 Resolve-by-state-change reused verbatim over label terms (≥2 terms per
      contributing destination); test: destinations created → quiet; one deleted →
      advisory returns.

- [x] 2.4 Staleness and robustness: the accessor returns `parent_generation`;
      a page whose stored generation differs from the current parse generation
      is not judged (covers partial vector coverage too); a malformed vector
      blob drops its row rather than silencing the sweep; v1
      `CLUSTER_MIN_TERMS` applies to the post-routing surviving label.

## 3. Delivery through existing machinery

- [x] 3.1 Audit category `scope_divergence_semantic` registered; findings keyed and
      fingerprinted as page identity + sorted label terms; the review-item composer is
      the single fingerprint authority (no second composition site).
- [x] 3.2 S6 integration: family appears in `registered_families()`; disposition
      `off`/`quiet` honoured; dismissed fingerprint stays dismissed until the label
      set changes (material-change reopen test).
- [x] 3.3 Bootstrap contract clause in the FULL contract only: destination choice
      is a pre-write responsibility (search or create a focused destination when a
      coherent durable thread emerges). Compact payload byte-identical — the
      24-byte headroom is pre-committed to the queued compact-bootstrap trim
      (design D8); the compact-budget test passes unmodified.

## 4. Gates

- [x] 4.1 Focused suites: the new sensor tests, audit suite, review-state/dispositions
      suites, attention suite, `tests/test_epistemic_bootstrap_contract.py`,
      `tests/test_bootstrap_compact_budget.py`. Mutation proof: each gate constant and
      the disclosure/exclusion rules each name a test that fails when deleted
      (scratch-copy mutations, never the worktree).
- [x] 4.2 Cost bound: sensor pass over the largest sample-vault page measured and
      recorded; corpus sweep amortization asserted: exactly one vector-bearing
      `semantic_unit_vectors` load per audit run, never a per-page query.
      Measured (implementer, WSL2, box shared with another lane): the shipped
      sample vault's largest page carries ONE semantic unit, so it bounds
      nothing — synthetic worst cases stand in. `detect()` 0.80 ms at 100 units
      and 9.6-10.1 ms at the 400-unit cap (the O(units^2) term the cap exists to
      hold); `all_semantic_unit_vectors()` 3.6 ms for 450 rows across 50 pages,
      paid ONCE per sweep; full sweep 40.9-42.7 ms for 50 pages with every page
      firing, i.e. ~0.85 ms/page including the canonical per-page re-parse.
      Reviewer's independent figures on other hardware: ~67 ms at the 400-unit
      cap and ~123 MB resident at 40k units — the load is materialised whole, so
      memory scales with corpus size, not with the number of pages judged.
      Both sets are worth reading together: the category is in `ALL_CATEGORIES`,
      so it runs on every default `audit()` call, not only when asked for.
- [x] 4.3 `uvx ruff check --select F`; `openspec validate --all --strict`; no
      tool-surface pin move (`git status` proof — descriptions unchanged).
- [x] 4.4 Lean suite once at delivery boundary, single process, quiet box.
