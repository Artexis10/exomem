## Context

How a governed write meets the vault mutation boundary today:

1. `writer_lease.invoke_command` runs surface admission and calls `LeaseManager.invoke`. Admission covers the cloud read-only refusal, the reserved-path preflight, the owner authority scope, the source-identity authority, foreground priority and store routing. The governance postfilter runs after the leaf.
2. `invoke` derives the retry identity in `_effective_idempotency_key`. It is an explicit key, an implicit MCP key (`mcp_retry_scope`, 600 s window), or none. REST without `Idempotency-Key` and credential issuance carry none. `invoke` computes the command digest from the keyword arguments.
3. `_run_absorbing_capture_contention` runs `IdempotencyStore.run`, which takes `operation_guard` and runs the leaf. Narrowed commands take only writer authority there. Their commit seam (`commit_creation`, `commit_existing`, `capture_source_artifacts_commit`) takes the boundary after preflight.
4. A pre-commit `MUTATION_BUSY` (`committed: false`) waits in a per-process line (`_FairCaptureQueue`). It holds one of half the sync workers for up to `EXOMEM_CAPTURE_WAIT_SECONDS` (40 s), capped by the request budget. Then it refuses with `holder_overdue`, `capture_wait_exhausted` or `capture_waiters_full`.
5. Three classes never wait: unkeyed remote writes, credential issuance, and open-world narrowed commands. Open-world commands run with `rerun_safe=False`, because a re-run fetches the files again. In-process CLI writes wait without a receipt (`absorb_unkeyed`).

Three more waits run before any write can be answered:

- The boundary acquire waits up to `EXOMEM_MUTATION_TIMEOUT` (5 s). This is why the refused captures took 5.2–5.6 s.
- The warm-up wait runs up to the capture wait bound (40 s).
- A retry whose key another worker holds waits up to 5 s on that claim.

How `IdempotencyStore` behaves, read in code and confirmed by a probe in a scratch state root:

- A claim moves a key from `reserved` to `executing` in one transaction. When the owner of an `executing` row is dead and no commit evidence exists, the next claim marks the key `MUTATION_OUTCOME_UNKNOWN` for good. A kill at any point after the claim, preflight included, therefore gives an unknown outcome, never a clean re-run.
- `_prune_expired` deletes completed rows by key prefix against the window of the current caller. An implicit key that a caller stored with a 24 h window is deleted when another implicit claim passes its 600 s window. The probe re-ran a completed leaf this way.
- A key whose stored digest differs from the new digest returns `IDEMPOTENCY_KEY_REUSED`.

Mechanisms this change reuses:

- `IdempotencyStore` (per vault and replica, SQLite under `LeaseConfig.state_dir`) is the commit authority. A keyed re-run replays a completed terminal, resumes from `GraphCommitReceipt` evidence, or reports `MUTATION_OUTCOME_UNKNOWN` for an abandoned owner.
- Maintenance jobs (`maintenance_jobs.py`) claim a row in `.maintenance-jobs.sqlite`. A daemon thread then runs the real leaf through `invoke_command` as the starting principal.
- Delegated principals: `upload_tokens._principal_from_values` rebuilds a `RequestPrincipal` from stored bearer-free values and forces `administrative_ingress=False`.
- Held uploads (`held_uploads.py`) keep private machine-local bytes behind an opaque `exomem-held:` reference. `capture_source` and `preserve_artifacts` redeem it without a network request.
- Fast acknowledgement (`accelerate-durable-write-acknowledgement`) makes every committed write prepare a `DerivedBatchReceipt` and publish a pending recall overlay.
- PR #2297 rechecks the basis guards of a write after a refused batch. The commit seam raises `MUTATION_BUSY` with `cause: auxiliary_changed` only when no basis guard moved.

Evidence from 2026-11-01:

- `logs/mutations.jsonl` holds 20 caller-visible busy rows between 07:40 and 08:08 UTC:
  - `capture_source` ×10, at 5.2–5.6 s after one acquire
  - `transfer_artifact` ×6, at 5.0 s, with no retry identity
  - `edit_memory` ×2, at 76.6 s and 125.1 s
  - `remember` ×2, at 59.4 s and 75.7 s
- The service log shows 150 `capture_source_artifacts_commit` holds between 07:00 and 09:00 UTC. Of these, 144 ran longer than 5 s and the longest ran 31.0 s. Together they held the boundary for 2,075 s, a mean of 13.8 s per hold.

## Goals / Non-Goals

**Goals:**

- No caller sees a busy or warming refusal for an eligible write, unless the intent journal cannot accept.
- Every eligible write waits at most 2.0 s before it gets an answer.
- Each accepted intent commits at most once, survives a crash, and never ends silently.
- The uncontended path does not change, captures included.

**Non-Goals:**

- Narrowing the vault mutation boundary (D9).
- One intent per capture file.
- Moving intents between machines or replicas.
- Push notifications.
- Changing semantic acceptance, the committed terminal or derived convergence.

## Prerequisites delivered by the capture-hold change

Two fixes need no journal and no flag. The capture-hold change delivers them, and this change depends on them:

- **Issuance narrowing.** `authorization_issuance` joins `narrow_boundary` in `writer_lease.py`. Held upload preparation writes only a private store under its own file lock. Plain upload only mints a token. Download makes a private temporary copy. Session open and rotate write `.governance.sqlite` in the vault state directory inside their own SQLite transaction. None of them writes vault-owned files, so none needs the boundary. This removes the six `transfer_artifact` refusals.
- **Partial-capture fix.** Files 2 and later of a capture take the boundary through `absorbing_mutation_guard`, which exists for local bytes that a retry reuses. (Inferred from code, not reproduced.) Today a busy refusal at file k escapes the per-file `except` in `capture_source_artifacts`. `_release_staged` then deletes the remaining bytes, and `invoke_leaf` reports committed-uncertain for the files that did commit.

## Decisions

### D1. The accept contract

An eligible write that cannot commit inline becomes an intent. Its response is one of three:

- The committed terminal, unchanged, when the intent commits before the accept deadline.
- The synchronous error, when an accept-time check fails.
- The accepted envelope:

    {"ok": true, "state": "accepted", "terminal": false, "status": "accepted",
     "committed": false, "mutated": false,
     "intent_id": "<opaque>", "intent_ref": "exomem://write/<intent_id>",
     "request_id": "...", "receipt_id": "...",
     "validation": "complete", "planned_paths": ["..."], "warnings_count": 0,
     "next_action": "review_memory(mode=\"write-intent\", ref=<intent_ref>, wait_seconds=30)",
     "poll_after_ms": 2000}

Field meanings:

- `validation` is `complete` when the command's own validation passed at accept. Otherwise it is `deferred` (D2).
- `planned_paths` are the targets that preflight resolved. The final path of a creation can still change through uniquify. The terminal names the final path.
- `poll_after_ms` tells a client when a poll is useful. The envelope carries no `retry_after_ms`, because a success must not invite a resend.
- `operation_id` keeps its meaning as the draft correlation (`mutation_terminal._operation_id`). The intent identifier is `intent_id`.

**The accept deadline.** `invoke` sets one context variable at the arrival of the request: the accept deadline. It is 2.0 s after arrival, or the request budget less the delivery reserve when that comes first. The owner chose 2.0 s on 2026-11-01 so that a contended write answers at once. It is a constant, not a knob, like the post-canonical budget of `durable-write-acknowledgement`.

Every wait before the answer honours the deadline:

- the inline boundary acquire, which caps `EXOMEM_MUTATION_TIMEOUT` at the deadline;
- the warm-up wait;
- the wait on an identical in-flight retry;
- the collection-store admission and queue wait;
- the wait for the intent to commit after the row commits.

A wait that reaches the deadline before the row exists ends as a pre-commit refusal, so the write journals. The deadline bounds waiting, not work. Preflight that the synchronous path also does, such as validation, a `validate_only` preview and file fetches, adds to the response time.

The wait after the row commits is 0 in two cases:

- No waiter slot is free.
- A remote write carries no retry identity. A short answer keeps its client's resend window short, because a resend is a new write.

The request thread waits on a condition that the applier signals, not on the lock. It polls every 250 ms when the applier runs in another process.

REST returns HTTP 202 with the success envelope for an accepted write. MCP returns a normal tool result. The CLI applies the same deadline before the row commits, and then waits for the intent with no bound, because an in-process caller loses no reply. Ctrl-C leaves the intent queued and prints its reference.

### D2. Run inline when free, journal on contention

The journal calls an intent **non-terminal** only when its state is `accepted` or `applying`. A `conflicted` intent waits for an owner decision and does not hold up other writes.

For an eligible command, `LeaseManager.invoke` does this:

1. Run today's pre-checks: the vocabulary binding and the warm-up wait, both under the accept deadline.
2. Look up the journal by retry identity (D3). If an intent with that identity exists within the replay window, return its state, or its terminal when it has one, and wait as in D1.
3. If the caller's principal audience has a non-terminal intent, journal before the leaf with `validation: deferred`. Do the same when the target argument names a page that a non-terminal intent targets. The caller's later write may depend on the earlier one: create A, then create B that links to A, or create a page and then edit it. Validation against current bytes would refuse such a write with an unresolved link or `NOT_FOUND`.
4. Otherwise run the write inline, as today, with one boundary acquire under the accept deadline.
5. If the attempt ends with a pre-commit refusal, record the intent and start the post-row wait of D1. The refusals are `MUTATION_BUSY` with `committed: false` (any cause, `intents_queued` included), `MUTATION_WARMING`, `REFERENCE_PREPARATION_UNAVAILABLE` and a wait that reached the accept deadline.

Step 3 uses the principal audience, not a finer identity. Two owner sessions therefore defer each other's validation while either has an intent waiting. That is the simpler rule. Its cost is a later rejection where an earlier synchronous error was possible.

**Arrival order across processes.** The journal is shared and the per-process line is not. So an eligible-command attempt checks for non-terminal intents twice:

- before it acquires the boundary: before the leaf for a wide command, and at the commit seam for a narrowed one;
- again once it holds the boundary.

If either check finds one, the attempt stops before any effect with an internal `MUTATION_BUSY`, `cause: intents_queued`, and step 5 journals the write. The caller never sees this refusal. `logs/mutations.jsonl` does not record it as a busy row; it records the `accepted` row instead.

The check applies only to attempts of eligible commands. Maintenance jobs, the graph drain, derived and projection holders, upload commits and ineligible commands take the boundary as today. They can therefore commit ahead of queued intents. The applier signals its wait to them (D4), so they step aside at their next unit.

**Validation at accept:**

- A narrowed command ran its preflight before its commit seam. A refusal at the seam therefore means validation passed: `validation: complete`.
- A wide command meets the boundary before its leaf. It runs its `validate_only` preview outside the boundary when it has one. The spec already allows previews outside the boundary. Without a preview, it gets `validation: deferred`.
- Step 3 and a warm-up that reached the accept deadline give `validation: deferred`.

A deferred intent that fails validation at apply ends `rejected` with the error that the synchronous path returns. An intent with complete validation ends other than committed only through a change to the vault after accept (D5), a conflicting use of its retry identity, or an owner decision.

**Captures run inline first.** `capture_source` and `preserve_artifacts` with files take the same path as other writes. An uncontended capture does not change. When the commit seam refuses file 1 before any commit, the capture leaf hands over instead of deleting its staged bytes:

1. Move each staged file (`StagedArtifact.path`) into the private input store of the journal, and check its sha256.
2. Replace each `download_url` with an internal input reference, which the leaf redeems like a held upload.
3. Record the whole capture as one intent.

The capture then fetches nothing again. Files 2 and later never journal, because the prerequisite makes them wait for the boundary with local bytes. A held upload in the arguments is copied into the input store at accept, so its one-hour expiry cannot strand the intent. The copy does not consume the held upload. A rejected or discarded intent therefore leaves the caller's held upload as it was.

One intent per capture keeps every argument of the capture, adoption mode and transcription options included. The applier commits a long capture as one intent, so later intents wait behind its files. Per-file intents need their own identity design and are out of scope.

Rejected: journal every eligible write, with no inline path. It changes the execution context of every uncontended write (principal, surface, budgets, spans, priority) for no gain to callers. The `always` rollout mode runs that shape in tests to exercise the applier.

### D3. The intent journal and the retry identity

**Location.** The journal is `<vault state dir>/.write-intents.sqlite` (`state_paths.vault_state_dir`), beside `.deferred-index.sqlite` and `.maintenance-jobs.sqlite`. Staged bytes go to `write-intents/inputs/<intent_id>/`. Both are machine-local and private, with the hardening of the idempotency store, and never under the synced vault. A hosted cell keeps them in its own state root.

**Format.** A `meta` table holds `format_version`. An applier that finds a newer version than it knows refuses to apply. Accept then answers `journal_unavailable`, and doctor names the version. Migrations run forward only.

**Durability.** The journal uses WAL with `synchronous=FULL`. The system writes, fsyncs and hash-checks staged inputs before the row commits. The row commits before the request starts its post-row wait.

**One row per intent.** Each row holds:

- `seq` (accept order) and `intent_id` (128-bit random);
- canonical vault identity and replica id;
- `state`: `accepted`, `applying`, `committed`, `rejected`, `conflicted` or `discarded`;
- command name, canonical arguments (`_canonicalize_command_kwargs`), surface descriptor, and the rewritten leaf arguments when D2 or D5 rewrote them;
- the principal snapshot (D4), which never holds a bearer, password or session secret;
- the retry identity, below;
- base guards: the caller-bound basis (`expected_hash`, `draft_token`, `draft_hash`, `transition_token`, `vocabulary_fingerprint`) and the stable reference of the target, as hashes or ids only;
- `validation`, planned paths, the content hash of each planned target at accept, input digests and sizes, accept and update times, attempt count, the last wait cause (a closed code), the time that cause started, the conflict record, and the compact terminal once the intent is terminal.

**The identity rule.** One rule decides which key commits an intent:

1. The row stores the key K that `_effective_idempotency_key` derived, the digest of the original arguments, the replay window, and a dispatch `request_id` minted at accept. A write without a retry identity gets K = `intent:<intent_id>` with the explicit 24 h window. A write with a retry identity keeps that key's own window (600 s for an implicit key).
2. A unique index covers K on rows in `accepted` or `applying`. The inline attempt inserts the row while it still holds its claim on K, before `IdempotencyStore` releases the claim as retryable. A second attempt that fails to insert under the same K returns the existing intent.
3. The applier claims K with the original digest. Rewritten arguments (input references, a moved target) go to the leaf only. A resend with the original arguments therefore never meets `IDEMPOTENCY_KEY_REUSED`.
4. Every dispatch of the intent uses the stored `request_id` as the active mutation request id. The collection store deduplicates on it in tranche 2.
5. The resend lookup of D2 also returns terminal intents within K's window. A resend after the intent ended gets its terminal or its review state, never a second intent.
6. `retry-write` mints a new key, `intent:<intent_id>:retry:<n>`, with the old key's window and a new `request_id`. The old key keeps its outcome.
7. A completed receipt for a journaled K, and the dedupe of a resend, last for K's own window. `IdempotencyStore` stores each row's expiry and prunes by it, not by the window of the current caller. This also fixes the prune that the probe showed for implicit keys. The journal row outlives the window as a content-free row for 24 h, for polls only.

**Order.** The applier applies by `seq`. A later intent for the same target applies after the earlier one, because order is global per vault.

**Retention.** A terminal intent drops its arguments and inputs at once. It keeps content-free fields and the compact terminal for 24 h after the terminal, for polls. A resend dedupes only within its key's own window. A `rejected` intent keeps its arguments for that window, readable by its own principal, and drops them when it ends. A `conflicted` intent keeps them until a decision.

**Orphan sweep.** A crash can leave staged inputs without a committed row. At start, and then every hour, the applier deletes each input directory whose intent is missing or terminal.

**Bounds.** These are configurable defaults. The values are my choice, not measured:

- non-terminal and `conflicted` intents per vault: 1,000 (`EXOMEM_WRITE_INTENT_MAX`);
- argument and input bytes per vault: 8 GiB (`EXOMEM_WRITE_INTENT_MAX_BYTES`);
- one principal audience: at most half of each bound;
- free space: accept leaves at least 1 GiB free on the state volume after staging.

The byte bound must be at least twice the largest input that one synchronous write accepts. (Inferred from code; task 1.1 confirms it.) Today that is the 2 GiB upload-session maximum, above the 100 MiB per fetched file. One audience's share then always holds at least one such write. The journal therefore never refuses a write for its own size that the synchronous path takes. Startup checks this relation and logs a warning when a configured bound breaks it.

**The only refusal.** Accept refuses only with `MUTATION_BUSY`, `status: retryable`, `committed: false` and `retry_after_ms`, with one of two causes:

- `journal_full` when a bound or the free-space floor is reached;
- `journal_unavailable` when the journal cannot open, write, fsync, pass its integrity check, read its format version, or find or start a live applier.

Undecided `conflicted` intents count toward the bounds. A pile of undecided conflicts therefore shows as `journal_full`, not as silent loss.

### D4. The applier

**Who.** One applier per vault applies intents. It runs in the process that holds `<journal>.applier.lock`, an OS lock that the process releases at exit. The service starts the applier with the server lifecycle and holds the lock. A CLI or library process that journals a write starts an applier only when no live applier holds the lock. A CLI applier releases the lock after each intent, so a service that starts takes over. The service is the preferred applier because a CLI applier runs other processes' intents with its own environment and code version. Doctor reports the applier's process kind and version.

**Entry point.** The applier dispatches through `writer_lease.invoke_command`, the same entry point as a request. A context variable, the applier dispatch, carries the `intent_id`, K, the original digest, the replay window and the dispatch `request_id`. With it set:

- `invoke_command` runs every admission check against the arguments that the leaf receives: the cloud read-only refusal, reserved paths, the owner authority scope, source-identity authority, foreground priority, store routing, and the postfilter after the leaf.
- `LeaseManager.invoke` claims K with the original digest, never journals, and skips the accept deadline.
- The commit seam does not refuse the applier with `intents_queued`. The store thread runs each call in a copy of the caller's context, so the exemption also holds there.

**Principal.** The row snapshots every field of `RequestPrincipal`: audience, surface, MCP session id, authorization session id, purpose, resolved flag, issuer family, verified authorization session, owner labels, client binding and origin session. These are all bearer-free. The applier rebuilds the principal with `upload_tokens._principal_from_values`, which forces `administrative_ingress=False`. Before each dispatch, the applier calls `auth_sessions.origin_session_active` for the snapshot. Today only the REST transport runs that check. A write from a revoked or expired login therefore does not apply (D5).

**Boundary wait.** The applier holds no request worker. It waits for the boundary as a loop of bounded attempts, each one an ordinary acquire. It never waits without bound inside writer authority, because a store release waits for every active authority guard to end. It raises the cross-process waiter signal, so background holders step aside at their next unit. An overdue holder raises an operator alarm in doctor and metrics, not a refusal. Each intent is one dispatch. The applier releases the boundary between intents, so other processes and background units interleave. This change does not batch intents.

**Per-intent bound.** Some wait causes belong to one intent: its reference is not prepared, a component it needs stays warming, or the cell is read-only. When such a cause lasts longer than the per-intent bound, the intent becomes `conflicted` with cause `stalled`, and later intents proceed. The default is 15 minutes (my choice). A held boundary and a lost writer lease block every intent alike, so they do not count toward the bound. The timer is the age of one unbroken wait on one cause: a change of cause restarts it, so a lost lease or a held boundary in between never adds to it.

**Exactly once.** K in `IdempotencyStore` is the commit authority. Recovery at each crash point:

| Crash point | Recovery |
|---|---|
| after the row commits, before the response | the intent applies; a resend with the same identity finds it |
| row `applying`, after the claim on K, before the canonical commit (preflight included) | the next claim finds an abandoned `executing` claim without evidence and gets `MUTATION_OUTCOME_UNKNOWN`; the intent becomes `conflicted` with cause `outcome_unknown` and is never re-run |
| after the canonical commit, before the terminal persists | `GraphCommitReceipt` evidence for K resumes the terminal; without evidence, `outcome_unknown` as above |
| after the terminal persists, before the row updates | the re-dispatch replays the terminal (rule 7 keeps the receipt); the row becomes `committed` |
| row `applying` and no receipt for K | `outcome_unknown`; never re-run, because the receipt may be gone |

At start, the applier scans non-terminal rows by `seq`. It probes the pid:nonce lock of the applier that owns an `applying` row, as the idempotency store probes its owners. When that owner is gone, the applier re-dispatches the row under K, and the table above decides.

So every kill during an apply becomes an owner decision. The review item carries the evidence to decide quickly: each planned target's content hash at accept and now, whether the log holds an entry with the dispatch `request_id`, and the retained arguments.

**Graceful shutdown.** On a stop request, the applier takes no new intent. It checks a stop flag before each boundary acquire, so an apply that has not reached the boundary ends with a pre-commit refusal. Its claim on K then ends retryable, and the row returns to `accepted`. An apply that holds the boundary runs to its terminal. The service joins the applier thread within its shutdown bound. Only a kill, or a leaf that outlasts that bound, leaves an `outcome_unknown` item.

**Writer lease (multi-host).** Only the writer-lease holder applies. The applier calls `ensure_writer` through `invoke`. `_maybe_idle_release` counts non-terminal intents as activity, so a replica with queued intents keeps the lease. A fenced apply rolls back through the existing transaction. The intent returns to `accepted` with the wait cause `writer_lease`. Intents never move between machines. A replica that stays down keeps its intents until it returns.

(Inferred, not verified.) A preferred replica can reclaim the lease from a replica with queued intents. That replica then waits for the lease back, and the two can alternate. Preferred reclaim therefore treats the holder's non-terminal intents as activity too. Task 3.6 proves that two managers do not alternate.

**Derived work.** An applied intent is an ordinary committed write. The fast-ack session prepares its `DerivedBatchReceipt` under the boundary, and the pending overlay publishes it. No client waits for derived convergence, so the applier skips the derived acknowledgement wait. It must not do that through a short request budget. The collection store derives its completion deadline from `acknowledgement_budget_deadline`, so a budget of "now" would make every store apply return pending. The applier runs with a request budget equal to the per-intent bound, and skips the derived wait through a separate setting. The terminal records `derived_sync` as today.

**Collection-store writes (tranche 2).** `invoke_command` routes the write to the store thread. The store makes one attempt. A pre-commit busy refusal makes the applier try again. `MUTATION_ACKNOWLEDGEMENT_PENDING` makes it wait and re-dispatch under K, which replays or waits on the live claim. The dispatch `request_id` keeps the store's own deduplication stable.

### D5. Reconciliation on the fly

The applier re-runs the command with its original arguments against the current vault. That re-run is the re-plan:

- The applier plans the navigation files (index, log, catalogues) again on every attempt. An `auxiliary_changed` busy (PR #2297) means wait and re-run.
- An edit without `expected_hash` matches its old text against current content. It behaves as if the caller had sent the write a moment later.
- When the target path is missing, the applier resolves the stable reference recorded at accept. If it resolves to one current path, the applier rewrites the target argument for the leaf and re-runs. K and its digest do not change.

A closed table of error codes classifies a dispatch that does not commit. The table sits beside the codes. It is a closed protocol set, not vocabulary:

| Dispatch outcome | Result |
|---|---|
| pre-commit `MUTATION_BUSY` (any cause), `WRITER_FENCED` | wait and re-run |
| `MUTATION_WARMING`, `REFERENCE_PREPARATION_UNAVAILABLE`, `CLOUD_CELL_READ_ONLY` | wait and re-run, within the per-intent bound |
| `MUTATION_ACKNOWLEDGEMENT_PENDING`, a live claim on K, the store completion deadline | wait, then re-dispatch under K, which replays or waits on the claim |
| per-intent bound reached | `conflicted`, `stalled` |
| committed-uncertain (`_PostCommitOutcomeUncertain`) | `committed`, with the uncertain terminal; never retried |
| committed failure (`_CachedCommittedFailure`) | `committed`, with the recorded failure; never retried |
| `STALE_EDIT`, `STALE_SEMANTIC_WRITE`, `PATH_GUARD_CHANGED`, another caller-bound basis mismatch | `conflicted`, `basis_changed` |
| `STALE_VOCABULARY_BINDING` | `conflicted`, `vocabulary_changed` |
| target missing, and its reference does not resolve | `conflicted`, `target_missing` |
| the leaf returns `needs_review` (a review step is now required) | `conflicted`, `review_required` |
| semantic or governance rejection after complete validation | `conflicted`, `contract_changed` |
| the same rejection after deferred validation | `rejected`, with the synchronous error |
| `IDEMPOTENCY_KEY_REUSED` | `conflicted`, `identity_conflict` |
| `MUTATION_OUTCOME_UNKNOWN` | `conflicted`, `outcome_unknown` |
| the origin login or the client binding was revoked | `conflicted`, `authorization_revoked` |
| the authorization session expired | `conflicted`, `authorization_expired` |
| any other error | `conflicted`, `apply_failed` |

A `conflicted` intent is a write-conflict review item. Its state name differs from `needs_review` on purpose: `needs_review` is the draft handshake, which tells the caller to complete a review step and re-issue. A departed agent cannot complete that handshake, so a `review_required` item carries the handshake details for the owner only.

- Its reference is the intent reference. `review_memory(ref=...)` shows the cause, the target, the evidence (D4), the remediation and the retained arguments, projected for the current authority of the reader.
- `triage_memory(ref=..., action="retry-write")` puts the intent at the end of the line under a new key (D3, rule 6).
- For `basis_changed`, `retry-write` needs `rebase=true`. Rebase removes the hash guards (`expected_hash`, `draft_hash`, `vocabulary_fingerprint`), so the write applies to current content. A write whose basis is a draft or transition token cannot rebase. The owner discards it, and the agent prepares a new draft.
- `retry-write` is refused for `authorization_revoked` and `authorization_expired`. The caller re-sends under a live login, or the owner discards the item. The applier never runs a write with authority that the caller no longer holds.
- Only the owner may retry an `outcome_unknown` item. That retry is the owner's explicit choice to risk a duplicate. The applier never makes that choice.
- `triage_memory(ref=..., action="discard-write")` ends the intent `discarded` with a recorded reason.
- A discard, and a rejection at apply, release the vocabulary application binding that the intent created at accept. Without that release the binding stays `applying`, and later applications of that item refuse.
- The attention review and the recall status show the count of undecided write conflicts.
- The `triage_memory` write-intent actions are never journaled themselves.
- `retry-write` on an intent that holds a vocabulary binding first ends the old intent `discarded`, which releases its binding as a discard does. It then records the retry under the new key and never rebinds the old key.
- Rebase removes the hash guards wherever the call carries them: at the top level and inside the nested `operation` of `edit_memory`. Accept records the same guards from both places.
- Accept records the stable reference of each existing target in the base guards. A missing target that has a recorded reference resolves through it; one current path rewrites the leaf's target argument only.
- The applier dispatches through its own manager, the one that owns its journal, never the process manager. Two managers in one process stay apart.

**Order after a conflict.** When an intent becomes `conflicted`, a later intent for the same target applies before it. A `retry-write` then applies the earlier write after the later one. The review item says so when a later intent touched the same target.

### D6. How a caller sees completion

- **Wait in the request.** See D1.
- **Resend.** The same retry identity returns the current state of the intent, or its terminal, and waits as in D1. MCP clients that resend on a lost reply need nothing new. A REST client without `Idempotency-Key` that resends creates a second write, as today.
- **Poll.** `review_memory(mode="write-intent", ref=..., wait_seconds=N)` returns the state. Once the intent is terminal, it also returns the compact terminal, projected for the current authority of the reader as the synchronous path projects it. The request budget less the delivery reserve caps `wait_seconds`. A poll wait takes a waiter slot from the same cap as other request waits. With no free slot, the poll returns the current state at once. Without `ref`, the poll lists the reader's own non-terminal and `conflicted` intents, newest first, with a bound.
- **Status block.** Every recall and bootstrap response already carries a pending-coverage status. It gains the reader's counts of accepted and conflicted writes, so the next agent turn sees them.

This change adds no push notification. MCP clients do not reliably pass server notifications to the agent, and the status block reaches the agent on its next call.

### D7. Read-your-write

An accepted intent has no canonical bytes yet. A read never shows its content as current, because the intent can still end in a conflict.

- A direct read of a path that the reader's own non-terminal intent targets returns the canonical bytes and `pending_writes` (intent reference, command, state).
- Recall, search and browse add the reader's own pending intents to their status, with references and the planned paths that the reader may read.
- A reader sees only intents of its own principal audience. The owner sees all intents.
- After the intent commits, the fast-ack pending overlay gives direct and lexical read-your-write, as today. Reads consult the journal (disclosure), then canonical bytes, then the pending overlay, then the persistent catalogues.
- The journal read is best effort. A read first checks a cheap "no non-terminal intent" marker. When the journal cannot be read, the read returns without `pending_writes` and logs the failure. A read never fails or slows because of the journal.
- A caller that needs the new bytes waits with `review_memory(mode="write-intent", wait_seconds=N)` and then reads.

Rejected: a read barrier, where a direct read waits for the reader's pending intent. It would tie read latency to the boundary holder that the spec keeps reads away from. Rejected: a speculative overlay of accepted content. It would show a write that can still end in a conflict.

### D8. Which commands use it

Eligibility is one declared attribute on the registry `Command`, with a one-line reason per command. The narrowed-boundary list in `writer_lease.py` stays as it is. This change reports that list as debt.

- **Tranche 1 (the evidence):** `remember`, `replace_memory`, `edit_memory` (all sub-modes), `observe_memory`, `episode_memory` record, `manage_memory_file` create and append, `capture_source`, `preserve_artifacts`.
- **Tranche 2 (same change, after the tranche 1 proof):** move, delete, reclassify and recover; `link`; `schema_memory` save-relations; `maintain_memory` tag-variants and curation propose; collection-store writes (`record_memory`, `plan_memory`); and `/upload` commits.
- **Never journaled:**
  - Reads and `validate_only` previews.
  - Credential issuance: `transfer_artifact` upload and download, and `govern_memory` session open and rotate. The system cannot store a bearer for replay. The prerequisite already moves them off the vault boundary.
  - Maintenance jobs, which already return a job handle.
  - Boundary holders that are not caller writes: graph drain, derived and projection rows, and media reconciliation.
  - The `triage_memory` write-intent decisions.
- **In-process callers (CLI, terminal UI, library)** use the same accept path. After the row commits, their wait has no bound, and Ctrl-C leaves the intent queued. `exomem writes` lists, shows and resolves intents through the same `review_memory` and `triage_memory` modes.

### D9. Keep the global vault boundary

Recommendation: keep one boundary per vault in this change, and do not add per-page locks now.

- The journal removes the refusal that callers see, whatever the lock granularity. After it, the boundary is an internal ordering device that callers never meet.
- Almost every write also changes shared navigation files: index, log, catalogues and graph checkpoint. Per-page locks would still serialize on them, so they would also need optimistic re-plan of shared files. PR #2297 and D5 already give that re-plan at the commit seam.
- With a single applier, the transactional batch, rollback, fencing and derived-receipt proof stay as they are. Per-page locks need a footprint inventory for every writer, and they add lock-order risk.
- The boundary still limits throughput to one commit at a time per vault. For one owner and a few agents, callers feel queue age, not throughput, and this change measures queue age.

What would change this: oldest-intent age above 30 s at p90 under normal load after the shorter capture holds land, or a product move to multi-writer team vaults. The next change would then be per-target apply lanes behind the same journal, not per-page locks in front of it.

### D10. Observability

- `logs/mutations.jsonl` gains `cause` on busy rows. It also gains rows with outcome `accepted`, `applied`, `rejected`, `conflicted` and `discarded`, linked by `intent_id`. The internal `intents_queued` refusal is never a row.
- Metrics: intents by outcome, queue depth, oldest non-terminal age, accept-to-commit time, the share committed before the accept deadline, the time each request waited, and applier waits by cause.
- `exomem doctor` gains `write_journal`: depth, oldest age, undecided conflicts, format version, applier liveness, kind and version. It fails when the oldest `accepted` intent is older than 5 minutes in normal resource mode, or when no applier is alive while intents wait.
- Gauges for queue depth and oldest age are new to the metrics registry, which had counters and histograms only. A gauge is never restored after a restart. The doctor treats "no applier alive" as a failure only once the oldest accepted intent is 30 s old, so the gap between two intents of a CLI applier raises no alarm. The lock records no applier kind or version, so the check reports liveness only.
- Acceptance: over 7 days on the personal service, zero `MUTATION_BUSY` rows from eligible commands with a cause other than `journal_full` or `journal_unavailable`. Every accepted intent is counted as committed, rejected, conflicted, discarded or non-terminal.

## Risks / Trade-offs

- [Risk] Agents mishandle `accepted` and resend the write with a new identity. → Tool instructions and the scaffold reference describe `accepted` as success. The envelope carries `poll_after_ms`, not `retry_after_ms`. A real-model run with a held boundary checks that agents treat it so before `EXOMEM_WRITE_INTENTS=on` becomes the default.
- [Risk] Two execution paths, inline and applier. → The applier path runs for every contended write. `EXOMEM_WRITE_INTENTS=always` runs the write-path integration suites through the applier.
- [Risk] Every kill during an apply creates an owner decision. → Graceful shutdown finishes or aborts the current intent, and the review item carries the evidence to decide.
- [Risk] A stalled applier makes every contended write wait. → Doctor and metrics raise alarms. Setting the flag to `off` stops new intents while the applier keeps draining.
- [Risk] A long capture delays every later intent, because one capture is one intent. → The capture-hold change shortens holds. Queue age is measured, and D9 names the trigger for per-target lanes.
- [Risk] Content sits in machine-local state until apply. → Private paths, deletion at the terminal state, the orphan sweep, content-free telemetry, and a hosted cell keeps the journal in the cell.
- [Risk] A laptop that holds intents and stays off the network delays them. → The laptop keeps the writer lease while it has intents, and the status block shows them. Intents never move between machines.
- [Risk] Head-of-line blocking: an intent waits for an overdue holder, and every later intent waits behind it. → That is the state of the vault anyway. Doctor reports the holder. Causes that belong to one intent end at the per-intent bound.
- [Risk] A contended write runs preflight twice, at accept and at apply. → The corpus cache and census-token reuse keep the second pass cheap. Accept-to-commit time is measured.
- [Risk] A downgrade with queued intents strands them. → Rollback drains the journal first, and doctor fails while intents remain. A newer journal format refuses an older applier.
- [Unverified] Whether the Cloud gateway passes a `committed: false` success to its clients unchanged. This repository cannot check it.

## Alternatives rejected

- Longer waits only: a holder can outlast any wait. Waits hold workers and do not survive a restart.
- Per-page locks only: shared navigation files still serialize nearly every write, and long capture holds still cause refusals.
- Client-side retry: moves the problem to every client, and an unkeyed client commits twice.
- Lock-free optimistic commit everywhere: rewrites the transactional batch and rollback to get the re-plan that the commit seam already does.
- Intents inside the idempotency store: couples queue retention with replay windows in the store that holds commit authority. The journal points to a key instead.
- Intents in the synced vault: cross-replica ambiguity and watcher churn, for the same reason that derived receipts stay machine-local.
- Journal every write, or every capture with files: changes the uncontended path (D2).
- One intent per capture file: needs a per-item identity and a stored batch terminal under K. Out of scope.

## Migration Plan

1. Land the prerequisites: PR #2297, the capture-hold change with the issuance narrowing and the partial-capture fix, and the archive of `absorb-transient-write-busy`. Rebase the MODIFIED blocks on the resulting spec text.
2. Ship the journal, applier, status modes and doctor check with `EXOMEM_WRITE_INTENTS=off`. Nothing journals, and the new code is inert.
3. Run the end-to-end contention proof on a disposable vault copy with its own state root (tasks 10).
4. Set `on` for the personal service with tranche 1. Watch the refusal journal, queue age and doctor for 7 days.
5. Add tranche 2 behind the same flag, and repeat the proof cases for it.
6. Change the shipped default to `on` after the real-model run and 7 clean days. Remove the request-side contention loop for eligible commands.
7. To roll back, set `off` (writes return to today's busy behaviour), let the applier drain, check that `exomem writes` is empty, then roll the code back. The journal file stays inert.

## Open Questions

None blocking. The values of the bounds (D3, D4) are my choice and are tuned from the metrics after rollout.
