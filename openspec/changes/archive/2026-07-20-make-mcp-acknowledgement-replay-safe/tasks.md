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
- [x] 4.4 Commit and publish the isolated branch, then record the diagnosed mechanism, fix, verification, and remaining cross-replica risk in Exomem without altering the forensic notes.

## 5. Delivery Note (added 2026-07-20 during archive)

The code shipped through **PR #258 "harden writes and proactive entity capture"**
(released in **v0.25.0**), not through the isolated branch this task anticipated.
The branch `fix/mcp-ack-replay-safety` (tip `d85f4fd`, dated 2026-07-17) was left
behind, unmerged and never deleted, while its content reached `main` by another
route.

That combination is actively misleading and cost a full debugging session on
2026-07-20: a live-looking branch plus 23-of-24 ticked boxes reads as "fix
written but never shipped". It is not. Verified before archiving:

- `git diff origin/main origin/fix/mcp-ack-replay-safety -- src/ tests/` is
  **301 insertions against 4,417 deletions** — the branch is strictly *behind*
  `main`, not ahead of it.
- `main`'s `writer_lease.py` (1,170 lines) is a superset of the branch's (1,141).
  The only 6 lines the branch holds that `main` lacks are *older* forms of the
  same calls (`mutation_guard(subject)` without holder telemetry; the import
  without `active_mutation_snapshot`).
- `MUTATION_ACKNOWLEDGEMENT_PENDING` and `operation_guard` are present in `main`
  at the same counts as on the branch.

Rebasing the branch onto `main` produced 9 conflict hunks across 7 files, and
every single one resolved in `main`'s favour. That uniformity is the tell.

**Do not resurrect that branch.** If something must be recovered from it, the
tip is `d85f4fd` — but its content is a proven subset of `main`.

Remaining genuinely open (tracked separately, not by this change):
- The ergonomics follow-ups in `deferred-ergonomics.md` (items B, C, D).
- Item B's motivation is now evidenced: on 2026-07-19 an agent linked a relation
  by a slug it re-derived from the title. The write had truncated that slug, the
  relation resolved to nothing, and the write failed `SEMANTIC_CONTRACT_BLOCKED`.
