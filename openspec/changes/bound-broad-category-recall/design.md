## Context

The exact semantic-unit lane already has a complete, branch-preserving category/kind catalog. For an empty query, SQLite orders matching units by `updated DESC`, `parent_path DESC`, then `source_order ASC`, which is the same ordering applied after hydration. The current implementation nevertheless requests every match with `LIMIT 2147483647`, validates every matching parent, reparses every matching page, and only then slices to the caller's limit.

Live diagnosis separated the costs: fetching all 321 `decision` rows took about 2.6 ms and fetching three took about 0.14 ms, while validating 138 parents took about 147 ms and full hydration took about 472 ms warm. The repair must therefore bound parent validation and hydration without weakening DNF correlation, catalog readiness, access policy, or post-filter-before-limit behavior.

## Goals / Non-Goals

**Goals:**

- Make an exact, empty-query, limited category/kind unit request open work proportional to its requested result window when leading candidates are eligible.
- Preserve deterministic result order, exact DNF category/kind semantics, scope, access-policy enforcement, catalog readiness, and selected-parent generation validation.
- Continue past inaccessible, missing, or otherwise rejected candidates until the requested number of eligible hits is found or the catalog is exhausted.
- Prove the bound with high-cardinality tests and make the existing aggregate-only latency harness capable of preflighting a broad category.

**Non-Goals:**

- Changing the semantic catalog schema, category language, public tool contract, or ranking model.
- Optimizing non-empty hybrid/vector ranking in this change.
- Applying an early limit to page predicates, negation, unsupported predicates, or other plans that require canonical post-filtering.
- Consolidating all repeated parent reads or adding an order-covering SQLite index; measurements show those are secondary follow-ups.

## Decisions

### 1. Add a narrow ordered-prefix fast path

Use the fast path only when all of the following hold: the filter algebra is complete, the normalized query is empty, the caller supplied a finite limit, and `post_filter_required` is false. Query the existing catalog from the beginning with a prefix bounded initially by `max(8, requested_limit)`. If canonical rejection underfills the result, double the prefix and recompute eligibility from that complete prefix.

Each prefix retains the existing catalog readiness and current-parent validation behavior, then passes through canonical access policy and unit hydration. Stop once enough eligible records exist; final sorting and slicing remain unchanged. When the leading rows are eligible, a `limit=3` request validates and hydrates at most eight catalog rows regardless of whether the category has ten or ten thousand matches. Restarting at zero matters because ordinary live catalog writers may insert, delete, or reorder rows without advancing the persisted freshness checkpoint; an offset spanning two read transactions could otherwise skip or duplicate a candidate. A later prefix is self-contained after such a mutation.

Alternatives considered:

- A single `LIMIT requested_limit` query is faster but can underfill or false-empty when access policy rejects a leading parent.
- Offset pagination avoids repeated prefix work but is not request-stable across ordinary concurrent catalog mutations.
- Fetching and hydrating every match preserves correctness but is the measured bottleneck.
- Denormalizing all parent/access metadata into SQLite would make more filters pushdown-safe but adds schema and migration risk that this repair does not need.

### 2. Keep correctness-first exhaustive retrieval for post-filter plans

If `post_filter_required` is true, keep the complete candidate seed and hydrate/evaluate it before applying the caller's limit. This preserves the existing regression where more than one ranking window of newer drafts precedes the first active result. `limit=None` also retains complete retrieval semantics.

Progressive pagination for arbitrary page predicates remains a future optimization because it needs a broader canonical-filter and snapshot design.

### 3. Treat catalog readiness as the whole-set freshness authority

The fast path does not weaken the catalog readiness checkpoint or semantic projection identity. Each fetched prefix still uses the typed catalog result and selected-parent generation validation; any incomplete outcome remains `RETRIEVAL_INDEX_WARMING`, never an authoritative empty result. The maintained freshness checkpoint remains the authority for rows outside the selected prefix, as it already must be for notes that changed from a nonmatching category into the requested category.

### 4. Extend the privacy-safe harness with a broad-cardinality preflight

Keep the current exact-two-candidate profile as the default. Add an explicit broad profile that requires at least a configured candidate count and preflights with a sufficiently large untimed limit. Reports continue to expose only bucketed counts and aggregate latency distributions. The existing cold/hot latency thresholds then apply to the same fixed request shape against a genuinely broad category.

## Risks / Trade-offs

- **[Risk] Separate prefix reads can observe a concurrently changing catalog.** → Every expansion restarts at zero and recomputes the complete current prefix, so insertion, deletion, or reordering cannot move a candidate across an offset boundary. Typed incomplete outcomes still defer through the existing warming/repair behavior.
- **[Risk] Access exclusions may require multiple prefixes.** → Expand geometrically until the limit or exhaustion; work grows with rejected leading rows, which correctness requires, but no longer with unrelated trailing matches.
- **[Risk] The fast path accidentally reaches a post-filter plan.** → Gate it directly on planner output and retain the existing post-filter-before-limit regression alongside a new high-cardinality bounded-open regression.
- **[Risk] Wall-clock tests become flaky.** → Make the CI regression assert bounded parent opens and request shape; keep percentile timing in the explicit aggregate benchmark harness.

## Migration Plan

No data migration is required. Deploy as a retrieval-code and test-harness change. Rollback is a code revert; the catalog remains compatible.

## Open Questions

None for this change. Consolidating validation and hydration into one parent-byte read is a measured follow-up if selected-parent cost remains material after this bound lands.
