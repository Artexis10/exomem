## 1. Baseline and compatibility boundaries

- [x] 1.1 Audit current framework seams and establish the scoped FastMCP 3 baseline; nine auth/transport modules pass (101 tests).
- [x] 1.2 Review migration security boundaries independently and resolve actionable findings in the design and regression tests.

## 2. Runtime migration

- [x] 2.1 Add failing regression coverage for provider transport errors and both MCP generations, then pin FastMCP 4.0.3 with a reproducible frozen lock.
- [x] 2.2 Adapt sanitized stdio, protocol types and error constructors; verify malformed-carrier redaction, reconnect and HTTP/stdio authorization tests.
- [x] 2.3 Reconcile OAuth/provider HTTP contracts without changing keys or durable state; verify callback, refresh, revocation and migration tests.
- [x] 2.4 Verify public tools, hosted mounting and call-ledger behavior with camel-case compatibility disabled; remove only proven-obsolete compatibility patches.

## 3. Delivery verification

- [x] 3.1 Run durable-closure regression coverage and the completion-boundary test suite: final-commit CI run 34256127023 passes all core/harness shards and installed-wheel E2E; the isolated workflow verifies 21 successful calls and immediate read-your-write availability. Prior workflow latency gains are not attributed to this migration.
- [x] 3.2 Obtain independent final security review and runtime verification: 84 security/compatibility tests pass; an isolated 21-call durable workflow has no refusals/retries and correct final state; real legacy and modern stdio commits support immediate exact reads.
- [x] 3.3 Run lint, public-artifact privacy and strict OpenSpec validation: all pass, including all 190 specifications/changes with the repository-pinned OpenSpec 1.10.0. Ready PR #1157 merged as 96a18a06 with green CI and release instructions; deployment remains a separate release acceptance step.
