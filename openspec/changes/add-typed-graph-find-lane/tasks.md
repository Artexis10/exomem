# Tasks: add-typed-graph-find-lane

## 1. Sidecar read API + freshness token (src/exomem/epistemic_graph.py)

- [ ] 1.1 Add `generation` to `graph_meta`: initialize on create/rebuild, bump
      in `upsert_after_write` and `delete_after_remove` (single UPDATE inside
      the existing write transaction).
- [ ] 1.2 Add module-level `cache_token(vault_root) -> tuple | None`:
      `(schema_version, registry_hash, generation)` when `available`, else
      `None`. Cheap: one SELECT on `graph_meta`; no table scans.
- [ ] 1.3 Add `EpistemicGraphIndex.neighbors_for(seeds: list[str])` returning
      typed edges touching the seeds in both directions: two indexed queries
      (`src IN (...)`, `dst IN (...)`), joined to exclude placeholder targets;
      each row exposes (seed_rel, other_rel, relation_type, direction,
      family). Family comes from the relation registry lookup used elsewhere
      in this module — do not re-parse YAML per call.
- [ ] 1.4 Red-first tests (tests/test_epistemic_graph.py or new file):
      generation bumps on write/delete/rebuild; token None when unavailable;
      neighbors_for returns both directions, excludes placeholders, is
      deterministic; incremental token == rebuild token after equivalent
      content.

## 2. Typed lane in find (src/exomem/find_candidates.py)

- [ ] 2.1 In the `if graph:` block, branch on sidecar availability: typed
      mode calls `neighbors_for(graph_seeds)` once; fallback mode keeps the
      existing `outbound_wikilink_paths` loop UNTOUCHED (byte-identical
      contract — do not refactor it).
- [ ] 2.2 Typed mode: build `graph_ranking` grouped by family precedence
      (provenance/epistemic families first, `links_to`/unregistered last;
      within family: seed order, then edge order). Keep the existing rules:
      skip targets already in `primary_set`, dedup via `seen_target`, tally
      `graph_in_degree_by_path` for ALL targets.
- [ ] 2.3 Populate `graph_provenance_by_path` (new `CandidateBundle` field,
      default empty) for typed-surfaced targets only: relation_type,
      direction, seed_rel of the FIRST edge that surfaced the target.
- [ ] 2.4 Keep the timings spans (`graph.seeds/resolver/expand`) meaningful in
      both modes (typed mode may rename `graph.resolver` → `graph.sidecar`).
- [ ] 2.5 Red-first tests (new tests/test_find_typed_graph_lane.py):
      typed neighbour surfaces for conceptual query (spec scenario);
      inbound edge counts; placeholder excluded; family precedence ordering;
      fallback equivalence — same fixture vault, sidecar disabled via
      `EXOMEM_DISABLE_GRAPH_INDEX`, full fused ordering equals a pre-change
      snapshot (capture the snapshot by running the wikilink path directly).

## 3. Freshness key (src/exomem/find.py)

- [ ] 3.1 In `_freshness_key()`, when `mode in ("hybrid","vector")` — same
      guard as embeddings — and graph lane enabled, append
      `(".graph.sqlite", epistemic_graph.cache_token(vault_root) or "absent")`.
- [ ] 3.2 Tests: cached result invalidated by a relation-adding write; WAL
      mtime-only change does NOT evict (reuse the token, not mtime — assert
      token unchanged); typed→fallback flip changes the key.

## 4. Hit annotation (src/exomem/find_results.py + envelope)

- [ ] 4.1 Thread `graph_provenance_by_path` from the bundle to hit assembly;
      attach optional `graph` field {relation_type, direction, seed} on
      matching hits. No change to hits without provenance.
- [ ] 4.2 Tests: annotated typed hit carries the triple; non-graph hits
      byte-identical to pre-change shape (snapshot assert); fallback mode
      never annotates.

## 5. Gates (do NOT edit thresholds or golden files in this lane)

- [ ] 5.1 `uv run python -m pytest -q` green.
- [ ] 5.2 `uv run python -m pytest tests/test_latency_gate.py -q` green,
      thresholds untouched.
- [ ] 5.3 Extend tests/test_graph_lane_perf.py with a typed-mode timing case
      (assert within existing budget; no new threshold).
- [ ] 5.4 `uvx ruff check` clean on changed files.

## 6. Orchestrator-owned (guarded files — NOT for the executor lane)

- [ ] 6.1 New golden tier: typed-relation fixtures + expectations in
      tests/golden/ + tests/test_retrieval_golden.py wiring; legacy goldens
      unchanged and passing in fallback mode.
- [ ] 6.2 Scaffold `_Schema/SKILL.md`: revise the "`ask_memory`/`find`
      ordering is unchanged" line to describe typed-graph-aware ordering +
      annotation; scaffold-no-leak test stays green.
- [ ] 6.3 Docs: `docs/ranking-tuning.md` (lane description),
      `docs/comparison-basic-memory-graph.md` "What to improve next" #2
      marked delivered; ARCHITECTURE.md graph-lane paragraph.
- [ ] 6.4 `scripts/graph_value_benchmark.py`: add a recall-visibility
      dimension (typed neighbour reachable through plain `find`) to the
      Exomem-only fixture gate.
- [ ] 6.5 Before/after recall snapshot on the reference vault (desk-side),
      recorded in the change dir for the archive.
