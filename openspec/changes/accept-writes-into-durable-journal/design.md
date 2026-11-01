## Context

How a governed write meets the vault mutation boundary today:

1. `writer_lease.invoke_command` runs surface admission (read-only cell, reserved paths, remote maintenance) and calls `LeaseManager.invoke`.
2. `invoke` derives the retry identity in `_effective_idempotency_key`. It is an explicit key, an implicit MCP key (`mcp_retry_scope`, 600 s), or none. REST without `Idempotency-Key` and credential issuance carry none.
3. `_run_absorbing_capture_contention` runs `IdempotencyStore.run`, which takes `operation_guard` and runs the leaf. Narrowed commands take only writer authority there. Their commit seam (`commit_creation`, `commit_existing`, `capture_source_artifacts_commit`) takes the boundary after preflight.
4. A pre-commit `MUTATION_BUSY` (`committed: false`) waits in a per-process line (`_FairCaptureQueue`). It holds one of half the sync workers for up to `EXOMEM_CAPTURE_WAIT_SECONDS` (40 s), capped by the request budget. Then it refuses with `holder_overdue`, `capture_wait_exhausted` or `capture_waiters_full`.
5. Three classes never wait: unkeyed remote writes, credential issuance, and open-world narrowed commands. Open-world commands run with `rerun_safe=False`, because a re-run fetches the files again. In-process CLI writes wait without a receipt (`absorb_unkeyed`).

Mechanisms this change reuses:

- `IdempotencyStore` (per vault and replica, SQLite under `LeaseConfig.state_dir`) is the commit authority. A keyed re-run replays a completed terminal, resumes from `GraphCommitReceipt` evidence, or reports `MUTATION_OUTCOME_UNKNOWN` for an abandoned owner.
- Maintenance jobs (`maintenance_jobs.py`) claim a row in `.maintenance-jobs.sqlite` in the vault state directory. A daemon thread then runs the real leaf through `invoke_command` as the starting principal.
- Held uploads (`held_uploads.py`) keep private machine-local bytes behind an opaque `exomem-held:` reference. `capture_source` and `preserve_artifacts` redeem it without a network request.
- Fast acknowledgement (`accelerate-durable-write-acknowledgement`) makes every committed write prepare a `DerivedBatchReceipt` and publish a pending recall overlay.
- PR #2297 rechecks the basis guards of a write after a refused batch. The commit seam raises `MUTATION_BUSY` with `cause: auxiliary_changed` only when no basis guard moved.

Evidence from 2026-11-01:

- `logs/mutations.jsonl` holds 20 caller-visible busy rows between 07:40 and 08:08 UTC:
  - `capture_source` ×10, at 5.2–5.6 s after one acquire
  - `transfer_artifact` ×6, at 5.0 s, with no retry identity
  - `edit_memory` ×2, at 76.6 s and 125.1 s
  - `remember` ×2, at 59.4 s and 75.7 s
- The service log shows 150 `capture_source_artifacts_commit` holds between 07:00 and 09:00 UTC. Of these, 144 ran longer than 5 s and the longest ran 31.0 s. Together they held the boundary for 2,075 s.

## Goals / Non-Goals

**Goals:**

- No caller sees a busy or warming refusal for an eligible write, unless the intent journal cannot accept.
- A contended write that commits within today's wait returns today's response.
- Each accepted intent commits at most once, survives a crash, and never ends silently.
- The uncontended path does not change.

**Non-Goals:**

- Narrowing the vault mutation boundary (D9).
- Moving intents between machines or replicas.
- Push notifications.
- Changing semantic acceptance, the committed terminal or derived convergence.

## Decisions

### D1. The accept contract

An eligible write that cannot commit inline becomes an intent. Its response is one of three:

- The committed terminal, unchanged, when the intent commits within the accept wait.
- The synchronous error, when an accept-time check fails.
- The accepted envelope:

    {"ok": true, "state": "accepted", "terminal": false, "status": "accepted",
     "committed": false, "mutated": false,
     "intent_id": "<opaque>", "intent_ref": "exomem://write/<intent_id>",
     "request_id": "...", "receipt_id": "...",
     "validation": "complete", "planned_paths": ["..."], "warnings_count": 0,
     "next_action": "review_memory(mode=\"write-intent\", ref=<intent_ref>, wait_seconds=30)",
     "retry_after_ms": 2000}

Field meanings:

- `validation` is `complete` when the command's own validation passed at accept. Otherwise it is `deferred` (D2).
- `planned_paths` are the targets that preflight resolved. The final path of a creation can still change through uniquify. The terminal names the final path.
- `operation_id` keeps its meaning as the draft correlation (`mutation_terminal._operation_id`). The intent identifier is `intent_id`.

The accept wait reuses today's bound: `EXOMEM_CAPTURE_WAIT_SECONDS` (40 s) from the arrival of the request, capped by the request budget less the delivery reserve. The wait is 0 when no waiter slot is free. It is also 0 for a remote write without a retry identity, which keeps its client's resend window short. The request thread waits on a condition that the applier signals, not on the lock. It polls every 250 ms when the applier runs in another process.

REST returns HTTP 202 with the success envelope for an accepted write. MCP returns a normal tool result. The CLI waits with no bound, because an in-process caller loses no reply. Ctrl-C leaves the intent queued and prints its reference.

### D2. Run inline when free, journal on contention

For an eligible command, `LeaseManager.invoke` does this:

1. Run today's pre-checks: replay lookup, vocabulary binding and the warm-up wait.
2. Look up the journal by retry identity. If a non-terminal intent has that identity, return its state and wait on it (D6).
3. If the vault has no non-terminal intent, run the write inline as today, with one boundary acquire.
4. If the attempt ends with a pre-commit refusal, record the intent and start the accept wait. The refusals are `MUTATION_BUSY` with `committed: false`, `MUTATION_WARMING` and `REFERENCE_PREPARATION_UNAVAILABLE`.

A commit seam refuses a thread that is not the applier at once, with `MUTATION_BUSY` and `cause: intents_queued`, while the vault has non-terminal intents. This keeps arrival order across processes, because the journal is shared and the per-process line is not. The caller never sees this refusal, because step 4 journals the write.

Validation at accept:

- A narrowed command ran its preflight before its commit seam. A refusal at the seam therefore means validation passed: `validation: complete`.
- A wide command meets the boundary before its leaf. It runs its `validate_only` preview outside the boundary when it has one. The spec already allows previews outside the boundary. Without a preview, it gets `validation: deferred`.
- A write whose target already has a non-terminal intent would validate against bytes that the earlier intent changes. It gets `validation: deferred`.
- A write that met a warm-up past its bound gets `validation: deferred`.

A deferred intent that fails validation at apply ends `rejected` with the error that the synchronous path returns. An intent with complete validation ends other than committed only through a change to the vault after accept (D5) or through an owner decision.

Open-world captures skip step 3. `capture_source` and `preserve_artifacts` with files fetch before they commit, and a re-run fetches again. At accept, they stage each fetched file into the private input store of the journal and check its sha256. Then they replace each `download_url` with an internal input reference, which the leaf redeems like a held upload. The intent holds one item per file, in order. The applier commits one item per step, so other intents commit between the files of a long capture. A held upload in the arguments is redeemed into the input store at accept, so its one-hour expiry cannot strand the intent.

(Inferred, not reproduced.) Today, a busy refusal at file k of a multi-file capture can follow commits of files 1..k−1. It then surfaces as committed-uncertain, and the staged bytes of the other files are deleted. The reason is that `capture_source_artifacts` catches only `SafeFetchError`, `PreserveError` and `AddError` per file, and `_release_staged` runs in `finally`. Per-file items remove this path.

Rejected: journal every eligible write, with no inline path. It changes the execution context of every uncontended write (principal, surface, budgets, spans, priority) for no gain to callers. The `always` rollout mode runs that shape in tests to exercise the applier.

### D3. The intent journal

**Location.** The journal is `<vault state dir>/.write-intents.sqlite` (`state_paths.vault_state_dir`), beside `.deferred-index.sqlite` and `.maintenance-jobs.sqlite`. Staged bytes go to `write-intents/inputs/<intent_id>/`. Both are machine-local and private, with the hardening of the idempotency store, and never under the synced vault. A hosted cell keeps them in its own state root.

**Durability.** The journal uses WAL with `synchronous=FULL`. The system writes, fsyncs and hash-checks staged inputs before the row commits. The row commits before the request starts its accept wait.

**One row per intent.** Each row holds:

- `seq` (accept order), `intent_id` (128-bit random), and the item index and parent for capture items;
- canonical vault identity and replica id;
- `state`: `accepted`, `applying`, `committed`, `rejected`, `needs_review` or `discarded`;
- command name, canonical arguments (`_canonicalize_command_kwargs`), command digest and surface descriptor;
- a principal snapshot: audience, surface, client and origin-session bindings, and owner flags. It never holds a bearer, password or session secret.
- the retry identity: the namespaced key, digest and replay window that `_effective_idempotency_key` derived. A write without one gets a minted key, `intent:<intent_id>`, with the explicit 24 h window.
- base guards: the caller-bound basis (`expected_hash`, `draft_token`, `draft_hash`, `transition_token`, `vocabulary_fingerprint`) and the stable reference of the target. These are hashes or ids only, no content.
- `validation`, planned paths, input digests and sizes, accept and update times, attempt count, the last wait cause (a closed code), the conflict record, and the compact terminal once the intent is terminal.

**Order.** The applier applies by `seq`. The items of one capture keep their order. A later intent for the same target applies after the earlier one, because order is global per vault.

**Retention.** A terminal intent drops its arguments and inputs at once. It keeps content-free fields and the compact terminal for 24 h, for polls. A `rejected` intent keeps its arguments for those 24 h, readable by its own principal. A `needs_review` intent keeps them until a decision.

**Bounds.** These are configurable defaults. The values are my choice, not measured:

- non-terminal intents per vault: 1,000 (`EXOMEM_WRITE_INTENT_MAX`);
- argument and input bytes per vault: 2 GiB (`EXOMEM_WRITE_INTENT_MAX_BYTES`), and never more than the state volume holds;
- one principal audience: at most half of each bound;
- one entry: never smaller than the largest arguments and fetch that the synchronous path accepts. The journal therefore never refuses a write for size that the synchronous path takes.

**The only refusal.** Accept refuses only with `MUTATION_BUSY`, `status: retryable`, `committed: false` and `retry_after_ms`, with one of two causes:

- `journal_full` when a bound is reached;
- `journal_unavailable` when the journal cannot open, write, fsync, pass its integrity check, or find or start a live applier.

Undecided `needs_review` intents count toward the bound. A pile of undecided conflicts therefore shows as `journal_full`, not as silent loss.

### D4. The applier

**Who.** One applier per vault applies intents. It runs in the serving process that holds `<journal>.applier.lock`, an OS lock that the process releases at exit. The service starts the applier with the server lifecycle. A CLI or library process that journals a write starts one when no live applier holds the lock. It keeps the applier until the journal is empty or the process exits.

**Dispatch.** The applier calls `LeaseManager.invoke` through an internal parameter. The parameter carries the resolved retry identity, the principal snapshot, the surface descriptor and the `intent_id`. That call never journals again. Every existing check runs: reserved paths, writer authority, fencing at the atomic write, semantic revalidation, the vocabulary gate and the governance postfilter. (Inferred.) `vocabulary_application.bind` must accept a second call for the same key. Verify this.

**Boundary wait.** The applier waits with no deadline and holds no request worker. It loops on pre-commit refusals. It raises the cross-process waiter signal, so background holders step aside at their next unit. An overdue holder raises an operator alarm in doctor and metrics, not a refusal. Each intent is one boundary hold, or one per capture item. The applier releases the boundary between intents, so other processes and background units interleave. This change does not batch intents.

**Exactly once.** The retry identity is the commit authority. Recovery at each crash point:

| Crash point | Recovery |
|---|---|
| after the row commits, before the response | the intent applies; a resend with the same identity finds it |
| in `applying`, before the leaf commits | re-run with the key; the leaf commits once |
| after the canonical commit, before the terminal persists | `GraphCommitReceipt` evidence for the key resumes the terminal; without evidence the result is `MUTATION_OUTCOME_UNKNOWN`, and the intent becomes `needs_review` (`outcome_unknown`), never re-run |
| after the terminal persists, before the row updates | the re-run replays the terminal; the row becomes `committed` |

At start, the applier scans non-terminal rows by `seq`. It probes the pid:nonce lock of the applier that owns an `applying` row, as the idempotency store probes its owners. When that owner is gone, the row returns to the head of the line.

**Writer lease (multi-host).** Only the writer-lease holder applies. The applier calls `ensure_writer` through `invoke`. `_maybe_idle_release` counts non-terminal intents as activity, so a replica with queued intents keeps the lease. A fenced apply rolls back through the existing transaction. The intent returns to `accepted` with the wait cause `writer_lease`. Intents never move between machines. A replica that stays down keeps its intents until it returns.

**Derived work.** An applied intent is an ordinary committed write. The fast-ack session prepares its `DerivedBatchReceipt` under the boundary, and the pending overlay publishes it. The applier passes an acknowledgement deadline of "now", because no client waits for derived convergence. The terminal records `derived_sync` as today.

**Collection-store writes (tranche 2).** The applier dispatches through `invoke_command`, which routes the write to the store thread. The store thread makes one attempt and hands back a pre-commit busy refusal. The applier then tries again.

### D5. Reconciliation on the fly

The applier re-runs the command with its original arguments against the current vault. That re-run is the re-plan:

- The applier plans the navigation files (index, log, catalogues) again on every attempt. An `auxiliary_changed` busy (PR #2297) means wait and re-run.
- An edit without `expected_hash` matches its old text against current content. It behaves as if the caller had sent the write a moment later.
- When the target path is missing, the applier resolves the stable reference recorded at accept. If it resolves to one current path, the applier rewrites the target argument and re-runs.

A closed table of error codes classifies a re-run that does not commit. The table sits beside the codes. It is a closed protocol set, not vocabulary:

| Re-run outcome | Result |
|---|---|
| pre-commit `MUTATION_BUSY` (any cause), `MUTATION_WARMING`, `REFERENCE_PREPARATION_UNAVAILABLE`, `WRITER_FENCED` | wait and re-run |
| `STALE_EDIT`, `STALE_SEMANTIC_WRITE`, `PATH_GUARD_CHANGED`, another caller-bound basis mismatch | `needs_review`, `basis_changed` |
| `STALE_VOCABULARY_BINDING` | `needs_review`, `vocabulary_changed` |
| target missing, and its reference does not resolve | `needs_review`, `target_missing` |
| the leaf returns `needs_review` (a review step is now required) | `needs_review`, `review_required`, with the handshake |
| semantic or governance rejection after complete validation | `needs_review`, `contract_changed` |
| the same rejection after deferred validation | `rejected`, with the synchronous error |
| `MUTATION_OUTCOME_UNKNOWN` | `needs_review`, `outcome_unknown` |
| the client or session binding of the principal was revoked | `needs_review`, `authorization_revoked` (owner decides) |
| any other error | `needs_review`, `apply_failed` |

A `needs_review` intent is a write-conflict review item:

- Its reference is the intent reference. `review_memory(ref=...)` shows the cause, the target, the remediation and the retained arguments, projected for the current authority of the reader.
- `triage_memory(ref=..., action="retry-write")` puts the intent at the end of the line for a fresh re-run.
- `triage_memory(ref=..., action="discard-write")` ends it `discarded` with a recorded reason.
- The attention review and the recall status show the count of undecided write conflicts.

A retry after `outcome_unknown` is the owner's explicit choice to risk a duplicate. The applier never makes that choice.

### D6. How a caller sees completion

- **Wait in the request.** See D1.
- **Resend.** The same retry identity returns the current state of the intent and waits as in D1. MCP clients that resend on a lost reply need nothing new.
- **Poll.** `review_memory(mode="write-intent", ref=..., wait_seconds=N)` returns the state. Once the intent is terminal, it also returns the compact terminal, projected for the current authority of the reader as the synchronous path projects it. The request budget less the delivery reserve caps `wait_seconds`. Without `ref`, the poll lists the reader's own non-terminal and `needs_review` intents, newest first, with a bound.
- **Status block.** Every recall and bootstrap response already carries a pending-coverage status. It gains the reader's counts of accepted and conflicted writes, so the next agent turn sees them.

This change adds no push notification. MCP clients do not reliably pass server notifications to the agent, and the status block reaches the agent on its next call.

### D7. Read-your-write

An accepted intent has no canonical bytes yet. A read never shows its content as current, because the intent can still end in a conflict.

- A direct read of a path that the reader's own non-terminal intent targets returns the canonical bytes and `pending_writes` (intent reference, command, state).
- Recall, search and browse add the reader's own pending intents to their status, with references and the planned paths that the reader may read.
- A reader sees only intents of its own principal audience. The owner sees all intents.
- After the intent commits, the fast-ack pending overlay gives direct and lexical read-your-write, as today. Reads consult the journal (disclosure), then canonical bytes, then the pending overlay, then the persistent catalogues.
- A caller that needs the new bytes waits with `review_memory(mode="write-intent", wait_seconds=N)` and then reads.

Rejected: a read barrier, where a direct read waits for the reader's pending intent. It would tie read latency to the boundary holder that the spec keeps reads away from. Rejected: a speculative overlay of accepted content. It would show a write that can still end in a conflict.

### D8. Which commands use it

Eligibility is one declared attribute on the registry `Command`, with a one-line reason per command. The narrowed-boundary list in `writer_lease.py` stays as it is. This change reports that list as debt.

- **Tranche 1 (the evidence):** `remember`, `replace_memory`, `edit_memory` (all sub-modes), `observe_memory`, `episode_memory` record, `manage_memory_file` create and append, `capture_source`, `preserve_artifacts`.
- **Tranche 2 (same change, after the tranche 1 proof):** move, delete, reclassify and recover; `link`; `schema_memory` save-relations; `maintain_memory` tag-variants and curation propose; `triage_memory`; collection-store writes (`record_memory`, `plan_memory`); and `/upload` commits.
- **Never journaled:**
  - Reads and `validate_only` previews.
  - Credential issuance: `transfer_artifact` upload and download, and `govern_memory` session open and rotate. The system cannot store a bearer for replay. `upload_handoff.prepare_upload` writes only machine-local private state (read in code). Session issuance probably does the same (inferred; verify). Issuance therefore takes writer authority and its own store lock, not the vault boundary. That removes the six `transfer_artifact` refusals. If a command proves to write vault-owned state, it keeps the boundary. This change then names it as the one remaining busy path.
  - Maintenance jobs, which already return a job handle.
  - Boundary holders that are not caller writes: graph drain, derived rows and media reconciliation.
- **In-process callers (CLI, terminal UI, library)** use the same accept path. Their wait has no bound, and Ctrl-C leaves the intent queued. `exomem writes` lists, shows and resolves intents through the same `review_memory` and `triage_memory` modes.

### D9. Keep the global vault boundary

Recommendation: keep one boundary per vault in this change, and do not add per-page locks now.

- The journal removes the refusal that callers see, whatever the lock granularity. After it, the boundary is an internal ordering device that callers never meet.
- Almost every write also changes shared navigation files: index, log, catalogues and graph checkpoint. Per-page locks would still serialize on them, so they would also need optimistic re-plan of shared files. PR #2297 and D5 already give that re-plan at the commit seam.
- With a single applier, the transactional batch, rollback, fencing and derived-receipt proof stay as they are. Per-page locks need a footprint inventory for every writer, and they add lock-order risk.
- The boundary still limits throughput to one commit at a time per vault. For one owner and a few agents, callers feel queue age, not throughput, and this change measures queue age.

What would change this: oldest-intent age above 30 s at p90 under normal load after the shorter capture holds land, or a product move to multi-writer team vaults. The next change would then be per-target apply lanes behind the same journal, not per-page locks in front of it.

### D10. Observability

- `logs/mutations.jsonl` gains `cause` on busy rows. It also gains rows with outcome `accepted`, `applied`, `rejected`, `needs_review` and `discarded`, linked by `intent_id`.
- Metrics: intents by outcome, queue depth, oldest non-terminal age, accept-to-commit time, the share committed within the accept wait, and applier waits by cause.
- `exomem doctor` gains `write_journal`: depth, oldest age, undecided conflicts and applier liveness. It fails when the oldest `accepted` intent is older than 5 minutes in normal resource mode, or when no applier is alive while intents wait.
- Acceptance: over 7 days on the personal service, zero `MUTATION_BUSY` rows from eligible commands with a cause other than `journal_full` or `journal_unavailable`. Every accepted intent is counted as committed, rejected, `needs_review`, discarded or non-terminal.

## Risks / Trade-offs

- [Risk] Agents mishandle `accepted` and resend the write with a new identity. → The accept wait stays at 40 s, so most callers never see `accepted`. Tool instructions and the scaffold reference describe the state. A real-model run checks it before the default changes.
- [Risk] Two execution paths, inline and applier. → The applier path runs for every contended write. `EXOMEM_WRITE_INTENTS=always` runs the write-path integration suites through the applier.
- [Risk] A stalled applier makes every contended write wait. → Doctor and metrics raise alarms. Setting the flag to `off` stops new intents while the applier keeps draining.
- [Risk] Content sits in machine-local state until apply. → Private paths, deletion at the terminal state, content-free telemetry, and a hosted cell keeps the journal in the cell.
- [Risk] A laptop that holds intents and stays off the network delays them. → The laptop keeps the writer lease while it has intents, and the status block shows them. Intents never move between machines.
- [Risk] Head-of-line blocking: an intent waits for an overdue holder, and every later intent waits behind it. → That is the state of the vault anyway. Doctor reports the holder.
- [Risk] A contended write runs preflight twice, at accept and at apply. → The corpus cache and census-token reuse keep the second pass cheap. Accept-to-commit time is measured.
- [Risk] A downgrade with queued intents strands them. → Rollback drains the journal first, and doctor fails while intents remain.

## Alternatives rejected

- Longer waits only: a holder can outlast any wait. Waits hold workers and do not survive a restart.
- Per-page locks only: shared navigation files still serialize nearly every write, and long capture holds still cause refusals.
- Client-side retry: moves the problem to every client, and an unkeyed client commits twice.
- Lock-free optimistic commit everywhere: rewrites the transactional batch and rollback to get the re-plan that the commit seam already does.
- Intents inside the idempotency store: couples queue retention with replay windows in the store that holds commit authority. The journal points to a key instead.
- Intents in the synced vault: cross-replica ambiguity and watcher churn, for the same reason that derived receipts stay machine-local.
- Journal every write: see D2.

## Migration Plan

1. Land the prerequisites: PR #2297, the shorter capture holds, and the archive of `absorb-transient-write-busy`. Rebase the MODIFIED blocks on the resulting spec text.
2. Ship the journal, applier, status modes and doctor check with `EXOMEM_WRITE_INTENTS=off`. Nothing journals, and the new code is inert.
3. Run the end-to-end contention proof on a disposable vault copy with its own state root (tasks 11).
4. Set `on` for the personal service with tranche 1. Watch the refusal journal, queue age and doctor for 7 days.
5. Add tranche 2 behind the same flag, and repeat the proof cases for it.
6. Change the shipped default to `on` after the real-model run and 7 clean days. Remove the request-side contention loop for eligible commands.
7. To roll back, set `off` (writes return to today's busy behaviour), let the applier drain, check that `exomem writes` is empty, then roll the code back. The journal file stays inert.

## Open Questions

- Should the accept wait stay at 40 s, or drop to about 5 s once the real-model run shows that agents handle `accepted`? Recommendation: keep 40 s until that evidence exists.
- Who may retry an `outcome_unknown` conflict? Recommendation: the owner principal only.
