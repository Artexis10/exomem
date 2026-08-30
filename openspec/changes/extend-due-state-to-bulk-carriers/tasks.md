# Tasks — extend due-state carriage to the bulk-operation leaves

Every test lands red first (verbatim failing output recorded before the
implementation, then green).

## 1. Carriage

- [ ] 1.1 Wire the shared due-state release-plane helpers into the five leaf
  responders (`adopt_vault` mutating modes, `adoption_studio` mutating
  actions, `maintain_memory` fix/reconcile, `preserve_artifacts`,
  `process_media` mutating operations). Red-first per leaf: a mutating
  invocation whose writes change the counts carries exactly one block at the
  end; outcome keys byte-identical otherwise.
- [ ] 1.2 Batch-once and change-only pins: a multi-write apply emits at most
  one block; an invocation with unchanged totals emits none; the emission
  ledger records one delivery per block. Red-first against the ledger.
- [ ] 1.3 Read-only invocations (scan-only adopt, status/work-item actions,
  audit-only maintain, media status) carry no block; the legacy response
  detail omits it. Red-first.
- [ ] 1.4 Failure isolation: unreadable review state yields no block, the
  operation completes with its terminal unchanged. Red-first.

## 2. Acceptance

- [ ] 2.1 Response-contract check: if any of the five leaves' recorded
  contract or packaged digest moves, follow the documented two-phase rollout
  and record it here; otherwise record that no contract moved.
- [ ] 2.2 `openspec validate --all --strict` green; scoped suites green; full
  suite at the completion boundary attributed against the origin/main
  baseline.
