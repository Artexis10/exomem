# Tasks — extend due-state carriage to the operation leaves

Every test lands red first (verbatim failing output recorded before the
implementation, then green).

## 1. Carriage

- [ ] 1.1 Wire the terminal's due-state admission for each enumerated
  invocation (design D2), reusing the `due_state.block_for_write` family
  behind the `due_state_advisory` disclosure boundary. Red-first per leaf: a
  committing invocation whose writes change the counts carries EXACTLY ONE
  block reflecting the post-batch projection; outcome keys byte-identical
  otherwise. Each leaf is independently shippable behind its own test.
- [ ] 1.2 Batch-once, change-only, and ledger pins: a multi-write apply
  emits one block recorded once in the emission ledger; a committing
  invocation with unchanged totals emits none. Red-first against the ledger.
- [ ] 1.3 Negative pins: no-commit invocations (clean-vault `fix
  dry_run=false`, already-valid media, `process_media` `retry`), read-only
  and dry-run invocations (scan-only adopt, default dry-run `fix`, status
  and preview actions), and the legacy response detail all carry no block.
  Red-first.
- [ ] 1.4 Failure isolation: unreadable review state yields no block while
  the operation completes with its terminal unchanged; a partially failed
  invocation that committed at least one write still carries under
  change-only. Red-first both halves.
- [ ] 1.5 Projection-delta settlement per design D3: wire the batch-delta
  path for `maintain_memory` mutating modes or record evidence that the
  rebuild path covers them; pin that the served block reflects the
  post-batch projection, not a stale read. Red-first with a
  mid-batch-changing fixture.
- [ ] 1.6 Pre-wiring payload check per leaf: the terminal's compact rebuild
  preserves the leaf's response payload (adoption-run document, maintain
  summaries, media job payload including the `state` key collision). A
  payload the terminal would drop is a BLOCKER escalated as its own change —
  never silently worked around. Record the per-leaf result here.

## 2. Bench and contract closure

- [ ] 2.1 Invert — do not delete — the two zero-carrier tripwire pins in
  `tests/test_due_state_emission_capture.py` (their red run against the old
  expectation is this task's red-first evidence); update the f23 driver
  docstring's zero-carrier inventory; record the f23
  `counter_emission_not_repeated_per_write` flip `unsupported` → decided in
  this file. Record f26's before/after when amendment sequence 2 activates
  (nothing publishes meanwhile). No family, assertion, predicate, gate, or
  OpKind changes (design D5).
- [ ] 2.2 Response-contract check: if any of the five leaves' recorded
  contract or packaged digest moves, follow the documented two-phase rollout
  and record it here; otherwise record that no contract moved.
- [ ] 2.3 `openspec validate --all --strict` green; scoped suites green; full
  suite at the completion boundary attributed against the origin/main
  baseline.
