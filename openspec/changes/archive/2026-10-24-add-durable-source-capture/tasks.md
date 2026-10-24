# Tasks

## 1. Durable text capture

- [x] 1.1 Add canonical identity schema, normalized input digest and guarded reconciliation; prove conflicts, distinct records, revisions and metadata updates in capture tests.
- [x] 1.2 Enforce existing namespace and creation authority before lookup and commit; prove restricted and incomplete lookup outcomes without writes.
- [x] 1.3 Document optional canonical fields and created/reused outcomes; verify the same stable reference after restart, expired replay and concurrent calls.

## 2. MCP retry boundary

- [x] 2.1 Consume explicit keys from namespaced MCP call metadata and pass verified principal scope to the retry engine; prove lost responses, mismatch rejection and principal isolation.
- [x] 2.2 Document the request metadata shape and prove real Cloud MCP capture across bearer rotation with network and models blocked.
- [x] 2.3 Regenerate discovery projections with maintained tools and verify schema fidelity and existing byte budgets.

## 3. Integrated proof and closure

- [x] 3.1 Run the coordinated full test corpus, lint, package build and privacy checks; record actual results for delivery.
- [x] 3.2 Validate the OpenSpec tree strictly, synchronize and archive through the CLI, and validate again while preserving existing requirements.

CI run `40055119246` passed all core and harness shards, lint, build, and installed-wheel checks.
The reviewed PR passed 33 checks; 13 checks skipped.
Integrated checks passed 202 tests, and the final independent recheck passed 74 cases.
