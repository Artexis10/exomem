## 0. Prerequisites

- [ ] 0.1 Merge PR #2297. Proof: its own suites pass on `main`.
- [ ] 0.2 Delivered by the capture-hold change: add `authorization_issuance` to `narrow_boundary` in `writer_lease.py`. Proof: `transfer_artifact` upload and download, and session open and rotate, succeed while a second process holds the vault boundary.
- [ ] 0.3 Delivered by the capture-hold change: take the boundary for files 2 and later of a capture through `absorbing_mutation_guard`. Proof: a 3-file capture whose second commit meets a held boundary commits all 3 files once and returns committed, not committed-uncertain.
- [ ] 0.4 Archive `absorb-transient-write-busy`. Rebase the MODIFIED blocks on the resulting spec text. Proof: `openspec validate --all --strict` passes.

## 1. Intent journal

- [ ] 1.1 Add `write_intents.py`: schema with `meta.format_version`, WAL with `synchronous=FULL`, private paths, the staged-input store, the bounds with the audience share and the free-space floor, and retention. Confirm the largest synchronous input and check the byte-bound relation at startup. Proof: an intent written and then killed with SIGKILL reads back complete; a write at a bound returns `journal_full`; the stored row has no credential field; an applier that reads a newer format version refuses and accept answers `journal_unavailable`.
- [ ] 1.2 Add the identity columns (K, original digest, replay window, dispatch `request_id`) and the unique index on K over `accepted` and `applying` rows. Proof: two inline attempts with the same K under a held boundary record one intent, and both callers receive its `intent_id`.
- [ ] 1.3 Make `IdempotencyStore` store each row's expiry and prune by it. Give a journaled K an expiry no earlier than the end of the intent's retention. Proof: the scratch-state probe that re-ran a completed implicit key after another implicit claim now replays it.
- [ ] 1.4 Add the orphan sweep for staged inputs. Proof: an input directory without a row is deleted at applier start; one with an `accepted` row stays.
- [ ] 1.5 Add the `intent_eligible` attribute to the registry `Command`, with a reason per command, and declare tranche 1. Proof: a registry test fails when a write command has no declaration.

## 2. Accept path

- [ ] 2.1 Add the accept-deadline context variable, set at request arrival. Make the inline acquire, the warm-up wait, the same-key wait and the collection-store admission end at it. Proof: a second process holds the boundary for 1 s, then for 120 s; the first write returns committed, the second accepted; each request records a wait of at most 2.0 s; each response time is at most 2.0 s plus the uncontended response time of the same write on the same fixture.
- [ ] 2.2 In `LeaseManager.invoke`, add the journal lookup by K (non-terminal and terminal intents within K's window), the inline attempt, and journaling on pre-commit refusals. Insert the row before `IdempotencyStore` releases the claim on K. Proof: a resend after the intent committed returns its terminal and no second intent.
- [ ] 2.3 Add the ordering check for eligible-command attempts: before the boundary acquire and again under the boundary, with the internal `intents_queued` refusal. Keep that refusal out of `logs/mutations.jsonl`. Proof: a write that arrives behind a queued intent commits after it; a maintenance job and an ineligible command run while intents wait; the log holds no `intents_queued` row.
- [ ] 2.4 Journal before the leaf with `validation: deferred` when the caller's principal audience has a non-terminal intent, or when a non-terminal intent targets the same page. Proof: create page A (accepted), then create page B that links to A; B is accepted, not refused, and both commit in order.
- [ ] 2.5 For wide commands, run the `validate_only` preview before accept, or else accept with `validation: deferred`. Proof: an invalid draft under a held boundary returns its validation error and records no intent.
- [ ] 2.6 Add the accepted envelope with `poll_after_ms`, and REST 202. Proof: MCP and REST contract tests on the envelope fields; the envelope has no `retry_after_ms`.

## 3. Applier

- [ ] 3.1 Add the applier lock, the preference for the service as applier, the FIFO loop, the boundary wait as a loop of bounded attempts, the waiter signal and the release between intents. Start the applier from the service lifecycle and from a journaling CLI. Proof: with two processes, one applier applies all intents in acceptance order; a service that starts while a CLI applies takes the lock after the current intent.
- [ ] 3.2 Dispatch through `invoke_command` with the applier-dispatch context variable. Claim K with the original digest, pass rewritten arguments to the leaf only, and exempt the applier from `intents_queued` on the store thread too. Proof: an applied intent whose target is a reserved path, or whose cell turned read-only, gets the same refusal as the synchronous path; a resend with the original arguments after a moved-target rewrite gets the intent, not `IDEMPOTENCY_KEY_REUSED`.
- [ ] 3.3 Snapshot every `RequestPrincipal` field, rebuild with `upload_tokens._principal_from_values`, and call `auth_sessions.origin_session_active` before each dispatch. Proof: an intent whose origin login was revoked after accept becomes `conflicted`, `authorization_revoked`, and the page is unchanged; an intent whose authorization session expired becomes `authorization_expired`.
- [ ] 3.4 Add crash recovery for the five rows of the crash table. Proof: one fault-injection test per row; each ends with exactly one commit (page content, one log entry, one receipt), or with an `outcome_unknown` item that carries the target hashes and the log evidence; no row re-runs a leaf after a claim.
- [ ] 3.5 Add graceful shutdown: the stop flag before each acquire and the bounded join. Proof: a service stop during an apply that waits for the boundary returns the intent to `accepted`, and the next applier commits it once with no review item.
- [ ] 3.6 Make idle release and preferred reclaim count intents, and make a fenced apply roll back and requeue. Proof: two `SQLiteLeaseStore` managers; a fenced apply leaves the page unchanged, the intent commits after the replica holds the lease again, and the lease does not alternate while intents wait.
- [ ] 3.7 Verify that `vocabulary_application.bind` accepts the applier's dispatch for the same key, and fix it if not. Release the binding on discard and on rejection. Proof: a keyed vocabulary application accepted under a held boundary commits once and records one applied state; after a discard, a new application of the same item commits.
- [ ] 3.8 Run the applier with a request budget equal to the per-intent bound, and skip the derived acknowledgement wait through a separate setting. Proof: a tranche 2 store write applied by the applier returns committed, not `MUTATION_ACKNOWLEDGEMENT_PENDING`.

## 4. Captures

- [ ] 4.1 Run captures inline first. On a pre-commit refusal at file 1, move the staged files into the input store, replace the fetch handles with input references, and record one intent. Copy held uploads into the input store without consuming them. Proof: an uncontended 3-file capture returns committed exactly as before; under a held boundary, a 3-file capture from a local HTTPS fixture is accepted, the fixture counts exactly 3 fetches, and the applier commits 3 sources once.

## 5. Reconciliation and decisions

- [ ] 5.1 Add the closed outcome table beside the error codes, and the target rewrite through the stable reference. Proof: one case per row with its own branch, including `MUTATION_ACKNOWLEDGEMENT_PENDING`, committed-uncertain and committed failure (`committed`, no retry), `IDEMPOTENCY_KEY_REUSED` and `CLOUD_CELL_READ_ONLY`.
- [ ] 5.2 Add the per-intent bound and the `stalled` cause. Proof: an intent whose reference never prepares becomes `conflicted`, `stalled`, after the bound, and a later intent then commits.
- [ ] 5.3 Add write-conflict review items with the decision evidence: `review_memory(ref)`, `triage_memory` `retry-write` (new key, `rebase=true` for `basis_changed`, owner only for `outcome_unknown`, refused for authorization causes) and `discard-write`, and the attention count. Proof: a hand edit under `expected_hash` produces an item; `discard-write` deletes the arguments; `retry-write` with `rebase=true` commits on current content under the key `intent:<id>:retry:1`.

## 6. Observation and reads

- [ ] 6.1 Add `review_memory(mode="write-intent")` with poll, wait and list, audience scoping, the projection for current authority, and the waiter-slot cap for poll waits. Proof: the owner sees all intents; another audience gets the unknown-reference answer; a poll with a wait returns the committed terminal; a poll with no free slot returns at once.
- [ ] 6.2 Add `pending_writes` on direct reads and the status counts in recall, bootstrap and browse, with the best-effort journal read. Proof: a read after `accepted` names the intent; a read with an unreadable journal succeeds without `pending_writes`; a search after the commit returns the new terms.

## 7. Surfaces

- [ ] 7.1 Describe `accepted` and `conflicted` in the MCP tool instructions and in `_Schema/references/mutation-results.md`. Proof: covered by 10.2.
- [ ] 7.2 Studio: show "saved, applying", poll, and show write conflicts. Proof: Chrome DevTools inspection of an action under a held boundary.
- [ ] 7.3 CLI: the accept deadline before the row, the unbounded wait after it, the Ctrl-C message, and `exomem writes list|show|retry|discard`. Proof: a CLI test with a held boundary and an interrupt.

## 8. Observability

- [ ] 8.1 Add `cause` and the intent rows to `logs/mutations.jsonl`, the metrics, and the doctor `write_journal` check. Proof: doctor fails on a fixture with a stale accepted intent and no live applier.

## 9. Cleanup

- [ ] 9.1 Remove the request-side contention loop for eligible commands: the request role of `_absorb_contention`, and `_FairCaptureQueue` and the capture waiter counters once no ineligible write uses them. Proof: the write-path suites pass in `on` and `always` modes; report the weight change as product lines against proof lines.

## 10. End-to-end proof (disposable vault copy, own state root)

- [ ] 10.1 Run a real service on a disposable vault. Hold the boundary from a helper process for 120 s. Then:
  1. Send an MCP `remember` with an implicit key. Expect `accepted` with a wait of at most 2.0 s.
  2. Resend the same call. Expect the same `intent_id`.
  3. Send an unkeyed REST edit. Expect HTTP 202.
  4. Create page A, then page B that links to A. Expect B accepted with `validation: deferred`, not refused.
  5. Send a keyed REST write, a Studio action and a CLI write. Expect committed or accepted, never busy.
  6. Read a page after its edit was accepted. Expect `pending_writes` to name the intent.
  7. Hand-edit a page under an accepted `expected_hash` edit. Expect a write-conflict item.
  8. Release the boundary. Expect each intent to commit once, in order.
  9. Send a SIGKILL to the service after a canonical commit, then restart it. Expect one commit or one `outcome_unknown` item, and no lost intent.
- [ ] 10.2 Real-model run: an agent's write meets a held boundary and receives `accepted`. Expect: the agent reports the write as pending or polls, and does not resend it with a new identity. Add each failure as a permanent regression case. The measurement spend rule applies.
- [ ] 10.3 Journal-full drill: with a small bound, a write returns `MUTATION_BUSY` with `journal_full`, and nothing is written.

## 11. Rollout and closure

- [ ] 11.1 Set `on` for the personal service (tranche 1). Expect over 7 days: zero busy rows from eligible commands other than journal causes, and a clean doctor.
- [ ] 11.2 Add tranche 2 and repeat 10.1 for its commands.
- [ ] 11.3 Change the default to `on`. Sync the deltas and archive the change. Run `openspec validate --all --strict` before and after.
