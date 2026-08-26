# Tasks

## 1. Regression tests (red first)

- [ ] 1.1 Reproduce the escalation: a batch write whose index upsert is
      refused by a held publication barrier records a full-index refresh
      demand today — pin the current behavior red-first against the fix.
- [ ] 1.2 Assert the fixed path records a path-scoped demand naming exactly
      the batch's changed/deleted paths, and that the bounded repair drains it
      without an O(vault) walk or full rebuild.
- [ ] 1.3 Assert the fail-closed branch: when the incomplete set cannot be
      named, a full refresh is still demanded and counted in telemetry.

## 2. Implementation

- [ ] 2.1 Replace the full-index refresh recording in the
      `batch_atomic_write` incomplete-upsert path with a path-scoped durable
      demand.
- [ ] 2.2 Drain path-scoped demands through `retry_deferred_upsert` /
      the single-flight repair owner's targeted mode.
- [ ] 2.3 Add stable content-free counters for targeted vs full-refresh
      demands.

## 3. Verification and delivery

- [ ] 3.1 Focused suites, lint, privacy, strict OpenSpec validation.
- [ ] 3.2 Independent adversarial review; resolve findings.
- [ ] 3.3 Live acceptance: a client write burst during an active build no
      longer seeds follow-on whole-vault builds; readiness windows stay
      bounded to the causing build only.
- [ ] 3.4 Sync and archive this change after delivery.
