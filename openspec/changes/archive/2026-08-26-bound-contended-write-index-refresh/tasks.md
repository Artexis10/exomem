# Tasks

## 1. Regression tests (red first)

- [x] 1.1 Pin the measured escalation red-first: a batch write whose
      embeddings component defers with `deferred_warmup` AND durable covering
      receipts currently fails `full_upsert_succeeded` and mints a durable
      full-component receipt (`.deferred-index.sqlite` `full_upserts` rows).
      After the fix this pin inverts: report succeeds, no full receipt.
- [x] 1.2 Fail-closed pin: a warm-up deferral WITHOUT covering durable
      receipts still fails the report, still mints the full-component demand,
      and increments the escalation counter.
- [x] 1.3 Baseline debt pin (current behavior, kept green before AND after):
      a lexical batch upsert refused under a held publication barrier is
      swallowed — `full_upsert_succeeded` stays True, no durable demand of
      any kind is recorded, recovery is the process-local deferred registry.
      This measures the out-of-scope debt named in the proposal so it cannot
      drift silently.

## 2. Implementation

- [x] 2.1 Extend `full_upsert_succeeded`'s deferral carve-out: accept
      embeddings `deferred_warmup` when and only when the deferral durably
      covers the batch's semantic paths.
- [x] 2.2 Stable content-free telemetry: covered-deferral-accepted and
      uncovered-deferral-escalated counters with a public accessor, reset
      with the module's test-reset seam.

## 3. Verification and delivery

- [x] 3.1 Focused suites, lint, privacy, strict OpenSpec validation.
- [x] 3.2 Independent adversarial review; resolve findings.
- [x] 3.3 Live acceptance: a client write burst during an embedding warm-up
      window no longer seeds follow-on whole-vault builds or graph epoch
      rebuilds; readiness windows stay bounded to the causing build only.
- [x] 3.4 Sync and archive this change after delivery.
