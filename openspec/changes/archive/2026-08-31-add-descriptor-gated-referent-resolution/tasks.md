## 1. Red-First Qualifier Contract Coverage

- [x] 1.1 Restore the two pre-change fuzzy-name tests and add empty-qualifier corroboration coverage
- [x] 1.2 Add qualifier extraction, deduplication, multi-qualifier, trailing-topic, exact-edge, and 500-entity fan-out regressions and capture the expected failing run
- [x] 1.3 Add benchmark case R2 with the exact multi-topical query and retain the envelope fixture proving non-qualifier anchor knowledge is threaded

## 2. Qualifier-Gated Resolution

- [x] 2.1 Add the narrower pre-nominal `qualifiers` field while preserving wide descriptor attribute evidence and the exact empty-qualifier two-kind rule
- [x] 2.2 Hoist qualifier-bearing seed detection out of entity fan-out, pre-stem qualifiers, preserve exact-name edge ordering, and keep missing-anchor lookup fail-safe
- [x] 2.3 Parse at most one shared anchor cap only when qualifiers and graph traversal are active, and memoize anchor tokens by parsed content identity
- [x] 2.4 Update the synthetic fixture and benchmark expectations while preserving cases A-Q, case J graph-only behavior, and all metric floors

## 3. Verification

- [x] 3.1 Run the scoped referent, entity registry, and governance test gate from the worker brief
- [x] 3.2 Run the benchmark, Ruff, mypy, pinned OpenSpec strict validation, reviewer probes, and completion-boundary corpus check
- [x] 3.3 Inspect the final diff and task allowlist, record evidence in `.task/RESULT.md`, and commit only the intended scope
