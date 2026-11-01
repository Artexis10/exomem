## 0. Prerequisites

- [ ] 0.1 Merge PR #2297 and the capture-hold change. Archive `absorb-transient-write-busy`. Rebase the MODIFIED blocks on the resulting spec text. Proof: `openspec validate --all --strict` passes.

## 1. Intent journal

- [ ] 1.1 Add `write_intents.py`: schema, WAL with `synchronous=FULL`, private paths, the staged-input store, bounds and audience share, retention. Proof: an intent written and then killed with SIGKILL reads back complete; a write at the bound returns `journal_full`; the stored row has no credential field.
- [ ] 1.2 Add the `intent_eligible` attribute to the registry `Command`, with a reason per command, and declare tranche 1. Proof: a registry test fails when a write command has no declaration.

## 2. Accept path

- [ ] 2.1 In `LeaseManager.invoke`, add the journal lookup by retry identity, the inline attempt, journaling on pre-commit refusals, and the accept wait (condition, waiter slot, zero wait for unkeyed remote writes). Proof: a second process holds the boundary for 3 s and then for 120 s; the first write returns committed, the second accepted; neither returns busy.
- [ ] 2.2 Make commit seams refuse a thread that is not the applier at once while intents are queued (`cause: intents_queued`). Proof: a write that arrives behind a queued intent commits after it, in the order the log records.
- [ ] 2.3 For wide commands, run the `validate_only` preview before accept, or else accept with `validation: deferred`. Defer validation for the same-target and warm-up cases. Proof: an invalid draft under a held boundary returns its validation error and records no intent.
- [ ] 2.4 Add the accepted envelope and REST 202. Proof: MCP and REST contract tests on the envelope fields.

## 3. Applier

- [ ] 3.1 Add the applier lock, the FIFO loop, the internal dispatch parameter, the boundary wait without a deadline, the waiter signal and the release between intents. Start the applier from the service lifecycle and from a journaling CLI. Proof: with two processes, one applier applies all intents in acceptance order.
- [ ] 3.2 Add crash recovery for the four crash points. Proof: one fault-injection test per crash point; each ends with exactly one commit (page content, one log entry, one receipt), or with an `outcome_unknown` review item.
- [ ] 3.3 Make idle release count intents, and make a fenced apply roll back and requeue. Proof: two `SQLiteLeaseStore` managers; a fenced apply leaves the page unchanged, and the intent commits after the replica holds the lease again.
- [ ] 3.4 Verify that `vocabulary_application.bind` accepts the applier's second call for the same key, and fix it if not. Proof: a keyed vocabulary application accepted under a held boundary commits once and records one applied state.

## 4. Open-world inputs

- [ ] 4.1 Stage fetched files into the input store at accept, with one item per file. Redeem held uploads into the store, and put internal input references in the arguments. Proof: a 3-file capture from a local HTTPS fixture under a held boundary commits 3 sources once; the fixture counts exactly 3 fetches; another write commits between two items.

## 5. Reconciliation and decisions

- [ ] 5.1 Add the closed outcome table beside the error codes, and the path rewrite through the stable reference. Proof: one case per row with its own branch: wait, `basis_changed`, `vocabulary_changed`, `target_missing`, moved target, `review_required`, `contract_changed`, deferred rejection, `outcome_unknown`.
- [ ] 5.2 Add write-conflict review items: `review_memory(ref)`, `triage_memory` `retry-write` and `discard-write`, and the attention count. Proof: a hand edit under `expected_hash` produces an item; `discard-write` deletes the arguments; `retry-write` commits on current content.

## 6. Observation and reads

- [ ] 6.1 Add `review_memory(mode="write-intent")` with poll, wait and list, audience scoping, and the projection for current authority. Proof: the owner sees all intents; another audience gets the unknown-reference answer; a poll with a wait returns the committed terminal.
- [ ] 6.2 Add `pending_writes` on direct reads and the status counts in recall, bootstrap and browse. Proof: a read after `accepted` names the intent; a search after the commit returns the new terms.

## 7. Surfaces

- [ ] 7.1 Describe `accepted` in the MCP tool instructions and in `_Schema/references/mutation-results.md`. Proof: covered by 11.2.
- [ ] 7.2 Studio: show "saved, applying", poll, and show write conflicts. Proof: Chrome DevTools inspection of an action under a held boundary.
- [ ] 7.3 CLI: unbounded wait, the Ctrl-C message, and `exomem writes list|show|retry|discard`. Proof: a CLI test with a held boundary and an interrupt.

## 8. Credential issuance

- [ ] 8.1 Verify that `transfer_artifact` upload and download and `govern_memory` session open and rotate write only machine-local private state. Move them to writer authority plus their own store lock. Proof: issuance succeeds while a second process holds the vault boundary.

## 9. Observability

- [ ] 9.1 Add `cause` and the intent rows to `logs/mutations.jsonl`, the metrics, and the doctor `write_journal` check. Proof: doctor fails on a fixture with a stale accepted intent and no live applier.

## 10. Cleanup

- [ ] 10.1 Remove the request-side contention loop for eligible commands: the request role of `_absorb_contention`, and `_FairCaptureQueue` and the capture waiter counters once no ineligible write uses them. Proof: the write-path suites pass in `on` and `always` modes; report the weight change as product lines against proof lines.

## 11. End-to-end proof (disposable vault copy, own state root)

- [ ] 11.1 Hold the boundary from a helper process for 120 s while a 5-file capture holds it for 30 s per file. Meanwhile, write over MCP (implicit and explicit keys), REST (keyed and unkeyed), Studio and CLI. Expect: no busy refusal; every write committed or accepted; each intent commits once after release; resends return their intent; a read after `accepted` names the intent; a hand edit under `expected_hash` becomes a write-conflict item; a SIGKILL of the service mid-run loses no intent.
- [ ] 11.2 Real-model run: an agent's write meets a held boundary and receives `accepted`. Expect: the agent reports the write as pending or polls, and does not resend it with a new identity. Add each failure as a permanent regression case. The measurement spend rule applies.
- [ ] 11.3 Journal-full drill: with a small bound, a write returns `MUTATION_BUSY` with `journal_full`, and nothing is written.

## 12. Rollout and closure

- [ ] 12.1 Set `on` for the personal service (tranche 1). Expect over 7 days: zero busy rows from eligible commands other than journal causes, and a clean doctor.
- [ ] 12.2 Add tranche 2 and repeat 11.1 for its commands.
- [ ] 12.3 Change the default to `on`. Sync the deltas and archive the change. Run `openspec validate --all --strict` before and after.
