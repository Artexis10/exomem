## 1. Deterministic Regression Tests

- [x] 1.1 Add an in-flight same-identity replay test that pauses the original, proves the retry waits outside the mutation boundary, and asserts one leaf execution.
- [x] 1.2 Add terminal-persistence fault tests for lost acknowledgement and cancellation after commit.
- [x] 1.3 Add tests for pending-wait expiry, different-key pre-commit `MUTATION_BUSY`, key/payload mismatch, and explicit-key principal isolation.
- [x] 1.4 Add stable-principal tests for personal MCP bearer rotation and hosted retries with new request IDs.
- [x] 1.5 Add read-only `maintain_memory(mode="audit")` classification tests and retain the edge single-origin timeout/5xx regression tests.
- [x] 1.6 Add regressions for receipt-persistence failure, single pending cleanup, hosted audit overlap, request-scoped correlation, explicit receipt expiry, REST mutation fail-closed routing, and structured REST errors.

## 2. Receipt And Mutation Ordering

- [x] 2.1 Refactor idempotency reservation/replay ahead of the operation guard while keeping new mutations behind the writer lease and vault mutation boundary.
- [x] 2.2 Add bounded pending-result waiting and the distinct `MUTATION_ACKNOWLEDGEMENT_PENDING` error.
- [x] 2.3 Persist completed results, recognized committed failures, and generic post-commit uncertainty before releasing mutation authority, preserving receipts across post-persistence interruption.
- [x] 2.4 Bind explicit receipt namespaces to authenticated principal scope and change hosted implicit identity from request-scoped to principal-scoped.
- [x] 2.5 Bound exact explicit terminal receipt retention at 24 hours while keeping uncertain states fail-closed for reconciliation.

## 3. Surface Classification And Correlation

- [x] 3.1 Derive personal MCP retry scope from verified issuer/subject claims with narrow credential/session fallback.
- [x] 3.2 Classify maintenance audit as read-only in the common command classifier.
- [x] 3.3 Emit privacy-safe request, receipt, lock, replay, terminal-persistence, and interruption phase correlation.
- [x] 3.4 Bind one canonical UUIDv4 across MCP middleware, command invocation, canonical writer logs, and edge forwarding.
- [x] 3.5 Route REST, upload, hosted command, and other mutation-capable POSTs to one origin without fallback replay.
- [x] 3.6 Exempt hosted maintenance audit from the consistency/mutation boundary and publish structured mutation error fields in REST OpenAPI.

## 4. Verification And Delivery

- [x] 4.1 Run focused writer-lease, mutation-lock, command-surface, hosted-gateway, REST principal-isolation, and Cloudflare worker tests.
- [x] 4.2 Run changed-file Ruff, scaffold leak checks, semantic/writer/watcher/reconcile/HA suites, and the full non-embedding suite; rerun the one path-length-sensitive Unix-socket case under a shorter secure temp root.
- [x] 4.3 Perform independent adversarial review and verification of cancellation, crash, failover, and compatibility boundaries.
- [ ] 4.4 Commit and publish the isolated branch, then record the diagnosed mechanism, fix, verification, and remaining cross-replica risk in Exomem without altering the forensic notes.
