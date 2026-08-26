## Why

Records is shipped, but its release gate proves the product path only through in-process command calls: the installed-wheel stdio product loop never invokes `record_memory`. At the same time, one contended GitHub runner stretched the serial suite from its normal roughly sixteen minutes to thirty-nine minutes and exposed four tests whose assertions depended on host scheduling rather than product semantics.

## What Changes

- Extend the existing clean-wheel, real-stdio MCP product loop with one bounded Records journey covering manual file ownership, safe append, targeted update, structured query/derived view, opaque Planning evidence, restart persistence, direct-edit visibility plus audit-gap detection, governance, and ordinary-recall isolation.
- Close the discovered public-surface gap where `record_memory(action="inspect")` omits the already-specified, governance-projected opaque Planning descriptors even though the internal manifest projection supports them.
- Keep the detailed X3, vehicle, dataset, governance, mutation, and scale matrices at their current lower test layers; the installed-product journey is one representative release proof, not a duplicate exhaustive suite.
- Rewrite three wall-clock-sensitive tests and isolate one cleanup test from the production prune budget so mutation admission, serialization, boundary placement, and cleanup semantics are observed independently of runner speed.
- Bound the lean matrix with a pytest session deadline and a GitHub job deadline, and always publish slow-test/JUnit evidence for diagnosis.
- Explicitly defer suite pruning, coverage-topology changes, Python-version coverage reduction, `xdist`, and broad sharding to a measured follow-up.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `records`: Require a real installed-wheel/stdio Records release journey in addition to in-process acceptance.
- `install-readiness`: Require bounded, diagnosable lean-suite execution and semantic rather than runner-speed assertions in release-critical concurrency tests.
- `product-e2e`: Extend the canonical installed-wheel product loop with Records discovery, mutation/restart proof, and unresolved-remote fail-closed coverage.

## Impact

- Canonical OpenSpec: sync the already-shipped `close-technical-memory-gaps` delta requirements before modifying its `product-e2e` capability.
- Product proof: `scripts/e2e_product_loop.py` and focused test coverage around its Records phase.
- Product correction: the bounded Records inspection projection and its command/governance tests; no new action or tool is added.
- Deterministic tests: `tests/test_mutation_concurrency.py`, `tests/test_record_mutation_matrix.py`, `tests/test_shorten_critical_section.py`, and `tests/test_continuation_checkpoint.py`.
- CI: `.github/workflows/ci.yml` gains a bounded matrix run and always-uploaded timing/JUnit evidence.
- The existing `inspect` response is corrected additively to satisfy the accepted Records contract; there is no new action/tool, storage format, governance rule, dependency, migration, or runtime timeout change.
