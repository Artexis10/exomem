## Why

Explicit reconcile can become effectively quadratic on a large vault because each graph page reacquires a freshness-checked wikilink resolver, and invalidating the live freshness registry can make the watcher misclassify a later baseline install as a corpus-wide source edit. On the production vault this turned one repair into roughly twenty minutes of single-core graph work followed by 1,621 already-current deferred embedding receipts.

## What Changes

- Bound a full or batched epistemic-graph maintenance operation to one detached wikilink-resolver acquisition, while preserving freshness checks between separate operations.
- Treat installation of a missing freshness baseline as initialization, not filesystem drift.
- End explicit reconcile with an immediate on-disk freshness baseline instead of leaving the registry non-live until the next periodic watcher pass.
- Preserve conservative deferred-work behavior for genuinely stale or unverifiable embedding rows and preserve exact watcher fan-out for real source changes.
- Add scaling, parity, failure-ordering, and no-phantom-fanout regression coverage.

## Capabilities

### New Capabilities

- `bounded-index-maintenance`: Bounded resolver work and snapshot consistency for full and batched epistemic-graph maintenance.

### Modified Capabilities

- `live-index-freshness`: Missing-baseline and explicit-reconcile behavior no longer reports or dispatches corpus-wide phantom drift.

## Impact

Affected areas are the epistemic graph rebuild/refresh path, detached resolver acquisition, freshness registry reconciliation, explicit reconcile cleanup, the file-watcher reconciliation seam, and their tests. No public MCP, REST, CLI, Markdown, sidecar-schema, dependency, or model behavior changes.
