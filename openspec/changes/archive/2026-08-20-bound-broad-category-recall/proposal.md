## Why

Exact semantic-unit category retrieval currently fetches, validates, parses, and hydrates every matching parent before applying the caller's result limit. A broad category such as `decision` therefore takes hundreds of milliseconds even though SQLite can return the requested top three rows in well under a millisecond.

## What Changes

- Bound exact empty-query category/kind retrieval by the caller's result limit when the complete filter plan requires no canonical post-filtering.
- Hydrate only the selected catalog parents while preserving catalog readiness, stale-repair, deterministic ordering, DNF correlation, scope, and access-policy behavior.
- Retain the exhaustive correctness path for page predicates, negation, and other post-filter plans where early limiting could create a false empty.
- Add high-cardinality regressions and an aggregate-only latency gate that prove parent work tracks the requested limit rather than category cardinality.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `find-recall-efficiency`: exact high-cardinality semantic-unit category/kind recall must bound selected-parent work without changing result order or post-filter correctness.

## Impact

- Retrieval planning and hydration in `src/exomem/find.py`.
- Exact semantic-unit catalog query coverage and category latency regression coverage in `tests/` and `scripts/category_recall_latency.py`.
- No public API, catalog schema, dependency, model, or migration change.
