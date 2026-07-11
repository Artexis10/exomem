## Why

Exomem now has a richer governed graph than Basic Memory, but the repository cannot yet prove that this produces better task outcomes. Before expanding schema or adding graph machinery, the project needs a reproducible comparison that fails when Exomem loses reachability and demonstrates strict advantages in semantic precision, provenance, lifecycle, and block-level context.

## What Changes

- Add a deterministic graph-value benchmark built from one product-neutral task manifest and semantically equivalent corpora rendered in each contender's native Markdown grammar.
- Measure graph-dependent outcomes separately: reachable-target recall, distractor precision, relation-type and direction fidelity, provenance traceability, supersession/active-conclusion handling, semantic-block precision, multi-hop completeness, response size, and latency.
- Define superiority as Pareto-style dominance rather than a weighted vanity score: Exomem must not lose baseline reachability and must be strictly better on the governed graph dimensions it claims as its moat.
- Provide a fast in-repository Exomem regression gate plus an optional desk-side Basic Memory MCP adapter pinned to a recorded version/commit. The external contender remains default-off and soft-fails with clear setup guidance when unavailable.
- Emit machine-readable JSON and aggregate Markdown reports containing the fairness contract, contender versions, per-dimension results, and any failed superiority criteria without including private vault content.
- Use benchmark failures to drive narrowly scoped graph fixes; do not add relation vocabulary, schema surface, models, or storage backends merely to improve the score.

## Capabilities

### New Capabilities

- `graph-value-benchmark`: Defines equivalent graph tasks, contender adapters, per-dimension metrics, dominance criteria, privacy-safe reporting, and regression behavior for demonstrating Exomem's graph advantage.

### Modified Capabilities

None.

## Impact

- Affected code: new benchmark/evaluation modules under `scripts/`, deterministic fixtures and tests, and benchmark documentation; graph runtime code changes only if a measured failure requires a focused fix.
- APIs: no public MCP, REST, or CLI contract change for the benchmark itself.
- Dependencies: no new required runtime dependency or reasoning model. Direct Basic Memory execution is an explicit desk-side option using an isolated home/config and recorded contender revision.
- CI: the small Exomem-only fixture gate remains model-free and dependency-light; cross-product and larger-corpus runs stay explicit desk-side jobs.
