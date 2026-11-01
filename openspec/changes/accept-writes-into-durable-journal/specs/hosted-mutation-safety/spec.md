## ADDED Requirements

### Requirement: Contended Writes Are Accepted Into A Durable Intent Journal

The system SHALL NOT refuse a write that is eligible for the intent journal because the vault mutation boundary is held, the waiting line or its worker slots are full, the holder is overdue, or a write-gating component is warming. It SHALL record the write as an intent in the vault's intent journal and answer within the accept wait. The accept wait SHALL start when the request arrives. It SHALL end at the configured write wait or at the request budget less its delivery reserve, whichever comes first. It SHALL be zero when no waiter slot is free or when a remote write carries no retry identity. When the intent commits within the accept wait, the response SHALL be the committed terminal that the synchronous path returns. Otherwise the response SHALL be the accepted envelope with the intent's `intent_id` and reference. An eligible write that finds no non-terminal intent for its vault SHALL first run inline, exactly as an ineligible write does. A commit seam that a thread other than the applier enters while the vault has non-terminal intents SHALL refuse that thread at once, before any effect, so that its write joins the journal behind them.

#### Scenario: A held boundary releases within the accept wait

- **WHEN** an eligible write meets a boundary that another process holds
- **AND** the holder releases before the accept wait ends
- **THEN** the caller receives the committed terminal that an uncontended write returns
- **AND** no `MUTATION_BUSY` reaches the caller

#### Scenario: A held boundary outlasts the accept wait

- **WHEN** an eligible write meets a boundary that stays held past the accept wait
- **THEN** the caller receives `state="accepted"`, `terminal=false`, `committed=false`, an `intent_id` and an intent reference
- **AND** the vault is unchanged until the applier commits the intent

#### Scenario: A write arrives behind queued intents

- **WHEN** an eligible write arrives while the vault's journal holds non-terminal intents
- **THEN** the system records the write behind them
- **AND** the write does not commit before them

#### Scenario: No waiter slot is free

- **WHEN** an eligible write meets a held boundary and every waiter slot is taken
- **THEN** the caller receives the accepted envelope at once instead of a refusal

### Requirement: Accept Runs Every Check The Caller Controls

Before it records an intent, the system SHALL run these checks: authorization; argument coercion and size limits; the read-only-cell and reserved-path checks; the idempotency replay lookup; the vocabulary-application identity gate; the fetch and digest checks of open-world inputs; and the command's own semantic and governance validation. A failed check SHALL return the error that the synchronous path returns, and the system SHALL record no intent. The system MAY accept with `validation="deferred"` only in three cases: validation needs a component that is still warming past the wait; a non-terminal intent already targets the same page; or the command has no validation preview and met the boundary before its leaf. An intent with deferred validation that fails validation at apply SHALL end `rejected` with the error that the synchronous path returns. An intent with complete validation SHALL end other than committed only through a change to the vault after accept or through an explicit decision.

#### Scenario: An invalid draft is refused at accept

- **WHEN** an eligible write fails its semantic validation while the boundary is held
- **THEN** the caller receives the validation error synchronously
- **AND** the journal holds no intent for it

#### Scenario: A second edit follows an accepted edit of the same page

- **WHEN** an edit arrives for a page that a non-terminal intent already targets
- **THEN** the system accepts the edit with `validation="deferred"`
- **AND** the applier validates it against the page after the earlier intent applies

### Requirement: The Intent Journal Is Durable, Machine-Local And Bounded

Each vault SHALL have one intent journal in its machine-local state directory, with private file permissions. The journal SHALL NOT be in the synced vault. An intent, with any staged input bytes, SHALL be durable before the system answers with it. An intent SHALL hold:

- the canonical command and arguments, with staged input references in place of fetch handles;
- a principal snapshot without any credential, and the surface;
- the retry identity;
- the caller-bound basis guards and the target references;
- the planned paths.

The applier SHALL apply intents in acceptance order. The journal SHALL bound non-terminal intents by count and by bytes per vault. It SHALL limit one principal audience to a share of each bound. It SHALL accept any single write whose arguments and inputs the synchronous path accepts. The only refusal of an eligible write SHALL be `MUTATION_BUSY` with `status="retryable"`, `committed=false` and `retry_after_ms`. Its `cause` SHALL be `journal_full` when a bound is reached, or `journal_unavailable` when the journal cannot record the intent durably or no applier can run. A terminal intent SHALL drop its arguments and inputs. It SHALL keep its content-free fields and its compact terminal for the replay window.

#### Scenario: The journal is full

- **WHEN** an eligible write meets a held boundary while the journal is at its bound
- **THEN** the caller receives `MUTATION_BUSY` with `cause="journal_full"` and `committed=false`
- **AND** nothing is written and the queued intents are unaffected

#### Scenario: The process stops after accept

- **WHEN** the serving process is killed after it answered `accepted`
- **THEN** the next applier applies the intent after it starts
- **AND** the intent commits once

#### Scenario: A credential never enters the journal

- **WHEN** the system records an intent for an authenticated remote caller
- **THEN** the journal holds the principal's audience and bindings
- **AND** it holds no bearer, password or session secret

### Requirement: One Applier Per Vault Commits Each Intent Once

At most one applier per vault SHALL apply intents. An operating-system lock, which its process releases at exit, SHALL choose that applier. The applier SHALL dispatch each intent through the shared command path with the intent's retry identity, principal snapshot and surface, so every existing check runs again. It SHALL wait for the boundary without a deadline and without holding a request worker. It SHALL signal its wait to background holders and SHALL release the boundary between intents. A write without a retry identity SHALL receive a server-minted identity that only its applier uses. After a crash, the applier SHALL resume non-terminal intents in order. When the applier cannot prove or exclude the commit of an intent, the intent SHALL become a decision. The applier SHALL NOT run that intent again automatically.

#### Scenario: Two processes serve one vault

- **WHEN** two processes on one host each have intents for the same vault
- **THEN** one applier applies all of them in acceptance order

#### Scenario: The applier stops after the canonical commit

- **WHEN** the applier process is killed after the canonical commit of an intent and before the intent records its terminal
- **THEN** the next applier replays or resumes the terminal from the receipt of the intent
- **AND** the vault holds exactly one copy of the change

#### Scenario: The commit cannot be proven

- **WHEN** recovery finds an intent whose receipt reports the outcome as unknown
- **THEN** the intent becomes a write-conflict review item with cause `outcome_unknown`
- **AND** the applier does not run it again until an explicit `retry-write` decision

#### Scenario: A resend finds its accepted intent

- **WHEN** a caller resends a write with the same retry identity while its intent is not terminal
- **THEN** it receives the current state and `intent_id` of that intent
- **AND** the system records no second intent

### Requirement: The Applier Respects Writer Authority

In a multi-host deployment, only the writer-lease holder SHALL apply intents. A replica with non-terminal intents SHALL NOT release the writer lease for idleness. An apply that loses its fencing token SHALL roll back through the existing transaction and SHALL return the intent to the line. The system SHALL NOT move an intent to another machine or replica.

#### Scenario: The lease is fenced during an apply

- **WHEN** the writer's fencing token changes while the applier commits an intent
- **THEN** the atomic write refuses the stale token and the transaction rolls back
- **AND** the intent returns to `accepted` and applies after the replica holds the lease again

#### Scenario: A replica with queued intents stays the writer

- **WHEN** a non-preferred replica has non-terminal intents and receives no request for longer than the idle-release time
- **THEN** it keeps the writer lease

### Requirement: Accepted Writes Reconcile On The Current Vault

The applier SHALL run each intent against the current vault. It SHALL plan navigation files again on every attempt. It SHALL apply a content-relative edit to current content. It SHALL follow a moved target through the stable reference recorded at accept when that reference resolves to one current path. The caller's own basis can stop holding in five ways: a caller-bound hash or token no longer matches, the target is deleted, the vocabulary binding changed, a review step is now required, or a contract now blocks the write. In each case the intent SHALL become a write-conflict review item with a closed cause code. The system SHALL NOT overwrite current content or drop an intent silently. A write-conflict item SHALL keep the intent's arguments readable by its principal and by the owner until a decision. A `retry-write` decision SHALL put the intent at the end of the line for a fresh attempt. A `discard-write` decision SHALL end it `discarded` with a recorded reason.

#### Scenario: An index changed while the intent waited

- **WHEN** another write changes the vault index and log while an intent waits
- **THEN** the applier commits the intent with freshly planned index and log entries

#### Scenario: The page changed under an explicit expected hash

- **WHEN** an accepted edit carries `expected_hash` and the page changes before the edit applies
- **THEN** the intent becomes a write-conflict review item with cause `basis_changed`
- **AND** the page keeps its current content

#### Scenario: The target page was deleted

- **WHEN** the target of an accepted edit is deleted and its stable reference no longer resolves
- **THEN** the intent becomes a write-conflict review item with cause `target_missing`
- **AND** its arguments stay readable until a decision

#### Scenario: The owner discards a conflict

- **WHEN** the owner triages a write-conflict item with `discard-write`
- **THEN** the intent ends `discarded` with the recorded reason
- **AND** the system deletes its arguments

### Requirement: Callers Observe Accepted Writes By Intent Reference

`review_memory(mode="write-intent", ref=...)` SHALL return the current state of an intent. Once the intent is terminal, it SHALL also return the compact terminal, projected for the current authority of the reader. It MAY wait up to `wait_seconds`, within the request budget less its delivery reserve. Without `ref`, it SHALL list the reader's own non-terminal and write-conflict intents, with a bound. A reader SHALL see only intents of its own principal audience, except the owner, who sees all intents. The recall and bootstrap status SHALL report the reader's counts of accepted and conflicted writes. The attention review SHALL report the count of undecided write conflicts.

#### Scenario: A caller polls until commit

- **WHEN** a caller polls an accepted intent with `wait_seconds`, and the intent commits during the wait
- **THEN** the poll returns `state="committed"` with the same compact terminal that an uncontended write returns

#### Scenario: Another audience polls an intent

- **WHEN** a principal polls an intent that a different non-owner audience accepted
- **THEN** the response is the same as for an unknown reference

### Requirement: Reads Disclose The Reader's Own Accepted Writes

A read SHALL NOT present the content of an intent that has not committed. A direct read of a path that the reader's own non-terminal intent targets SHALL return the canonical bytes and name that intent in `pending_writes`. Recall, search and browse SHALL add the reader's own non-terminal intents to their status. After the intent commits, the existing pending recall overlay SHALL provide read-your-write.

#### Scenario: A read follows an accepted edit

- **WHEN** a caller reads a page right after its edit of that page was accepted and not applied
- **THEN** the read returns the current canonical bytes
- **AND** `pending_writes` names the caller's intent and its state

#### Scenario: A search follows the commit

- **WHEN** the applier commits the intent and the caller then searches for its new terms
- **THEN** keyword recall returns the new content through the pending recall overlay

### Requirement: Credential Issuance Stays Synchronous Outside The Vault Boundary

The system SHALL NOT record a command whose result carries a bearer credential as an intent, and SHALL NOT replay it. When such a command writes only machine-local private state, it SHALL take writer authority and its own store lock instead of the vault mutation boundary.

#### Scenario: A transfer is prepared during a long capture

- **WHEN** a client prepares an upload transfer while a capture holds the vault boundary
- **THEN** the transfer returns its capability without waiting for the boundary

### Requirement: Caller-Visible Write Outcomes Carry Their Cause

The caller-visible mutation journal SHALL record the `cause` of every `MUTATION_BUSY` refusal. It SHALL record one row for each intent outcome (`accepted`, `applied`, `rejected`, `needs_review`, `discarded`), linked by `intent_id`. A hosted cell SHALL record no vault content in these rows.

#### Scenario: An operator audits refusals

- **WHEN** an operator reads the mutation journal for a day with contention
- **THEN** every busy row names its cause
- **AND** the accepted and applied rows of each intent share one `intent_id`

## MODIFIED Requirements

### Requirement: Lock Acquisition Fails Closed Before Mutation

The system SHALL acquire the vault mutation boundary before invoking a write-capable command leaf or committing an upload. If safe ownership cannot be obtained within the configured bound, the attempt MUST stop before creating, modifying, moving, or deleting any vault or sidecar file. A write that is not eligible for the intent journal MUST then fail with a stable machine-readable busy or unavailable error. The system SHALL instead record an eligible write as an intent and answer it as committed or accepted. An eligible write SHALL fail with a busy error only when the intent journal cannot accept it.

#### Scenario: Lock wait exceeds its bound

- **WHEN** a mutation that is not eligible for the intent journal cannot acquire the vault boundary before its wait deadline
- **THEN** the operation returns a stable retryable error
- **AND** its command leaf does not execute and the vault remains unchanged

#### Scenario: An eligible write cannot acquire the boundary

- **WHEN** an eligible write cannot acquire the vault boundary within its acquire bound
- **THEN** its attempt changes no vault or sidecar file
- **AND** the system records it as an intent and answers it as committed or accepted, not busy

#### Scenario: Lock backend cannot prove ownership

- **WHEN** the process-safe locking primitive is unavailable or returns an indeterminate ownership result
- **THEN** hosted mutation readiness fails closed
- **AND** no write is attempted under assumed ownership

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

All MCP mutations, including hosted mutations, SHALL preserve caller-supplied idempotency keys and bounded implicit retry replay through the existing common invocation boundary. Retry identity MUST include the resolved vault or tenant, authenticated principal scope, command, and canonical arguments. A key or implicit retry from one tenant MUST NOT replay or suppress a mutation for another tenant. An identical pending retry MUST inspect or wait on its receipt outside the exclusive vault mutation boundary, while different identities remain subject to normal serialization. An identical retry of a write that the intent journal holds SHALL return the current state of that intent and SHALL NOT record a second intent. Failed precommit mutations MUST become retryable, and committed terminal outcomes MUST replay without executing the leaf again.

A replay identity REQUIRES either an explicit caller-supplied idempotency key or a retry scope that is stable across the retry. Where neither resolves — an unauthenticated caller, or a stateless transport whose session identifier changes per request — the repeat carries no replay identity. The system SHALL treat the two requests as distinct by definition: no shared receipt is created, and no replay is offered. The system SHALL accept an eligible write of that kind as its own intent, under a server-minted identity that only the applier uses, so each request commits once. An ineligible write of that kind keeps normal serialization, up to and including `MUTATION_BUSY` for the second request. The replay guarantee above is therefore explicitly out of scope for that configuration rather than silently unmet.

#### Scenario: Gateway retries a completed hosted mutation

- **WHEN** the gateway repeats the same successful mutation for the same tenant, principal, command, canonical arguments, and idempotency identity after losing the acknowledgement
- **THEN** the original result is replayed without executing the mutation leaf again
- **AND** only one durable vault change exists

#### Scenario: Identical retry overlaps in-flight mutation

- **WHEN** an identical retry arrives while the first worker still owns the mutation boundary, or while the intent of the first request is not terminal
- **THEN** it waits on or inspects the matching pending receipt or intent outside the boundary
- **AND** it returns the terminal replay, the current state of the intent, or bounded `MUTATION_ACKNOWLEDGEMENT_PENDING`, never `MUTATION_BUSY` caused by competing with itself

#### Scenario: Retry arrives with no key and no stable scope

- **WHEN** a caller repeats a mutation with no explicit idempotency key while no stable retry scope resolves for it
- **THEN** the repeat resolves no replay identity, creates no shared receipt, and is not offered the first request's result
- **AND** an eligible repeat is accepted as its own intent and commits once
- **AND** an ineligible repeat contends for the vault mutation boundary like any other distinct mutation, up to and including `MUTATION_BUSY`

#### Scenario: Same key is presented for another tenant

- **WHEN** two tenant contexts present the same explicit idempotency key for otherwise identical input
- **THEN** each tenant resolves an independent idempotency record
- **AND** neither tenant receives the other's result or suppresses the other's mutation

#### Scenario: First attempt fails before commit

- **WHEN** a mutation raises during structural or semantic preflight before successful completion and the caller retries it
- **THEN** the pending receipt is removed or records a retryable precommit outcome
- **AND** the retry can acquire the boundary and execute normally

#### Scenario: First attempt fails

- **WHEN** a mutation raises before successful completion and the caller retries it
- **THEN** the failure is not replayed as a completed result
- **AND** the retry can acquire the boundary and execute normally

### Requirement: Keyed Writes Wait Out Warm-Up Within Their Request

A keyed governed write that arrives while a write-gating warm-up component is still warming SHALL wait for that component inside its request, within the bounded wait and the parked-worker cap that a busy boundary uses, and SHALL then run once. A write that is eligible for the intent journal SHALL NOT receive the warm-up refusal. When the bound runs out first, when no wait slot is free, or when the write carries no key, the system SHALL accept it as an intent with `validation="deferred"`. The applier SHALL apply that intent only after the component is ready. For a write that is not eligible, the system SHALL return the retryable warm-up refusal only when that bound runs out first or no wait slot is free, and the refusal SHALL report how long the write waited. An ineligible unkeyed write SHALL keep the immediate retryable refusal, because a resend after a client gave up during the wait would run it twice. An ineligible write attempted on a collection store's thread SHALL keep it too, because that thread never parks.

#### Scenario: A keyed write during a short warm-up commits

- **WHEN** a keyed governed write arrives while the semantic corpus is warming
- **AND** the corpus becomes ready within the bounded wait
- **THEN** the write runs once and commits
- **AND** the client receives no warm-up refusal

#### Scenario: An eligible write outlasts the warm-up bound

- **WHEN** an eligible governed write arrives while a write-gating component stays warming past the bounded wait
- **THEN** the caller receives the accepted envelope with `validation="deferred"` before its request deadline
- **AND** the applier commits the intent once after the component is ready

#### Scenario: A warm-up longer than the bound refuses

- **WHEN** a keyed governed write that is not eligible for the intent journal arrives while a write-gating component stays warming past the bounded wait
- **THEN** the write returns the retryable warm-up refusal before its request deadline
- **AND** the refusal reports the time waited
- **AND** the command leaf does not execute

#### Scenario: An unkeyed write is refused at once

- **WHEN** an unkeyed governed write that is not eligible for the intent journal arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting

#### Scenario: An eligible unkeyed write is accepted during warm-up

- **WHEN** an unkeyed governed write that is eligible for the intent journal arrives while a write-gating component is warming
- **THEN** it is accepted at once with `validation="deferred"`
- **AND** the applier commits the intent once after the component is ready

#### Scenario: A store-routed write does not park the store thread

- **WHEN** a keyed governed write that is routed to a collection store and not eligible for the intent journal arrives while a write-gating component is warming
- **THEN** it returns the retryable warm-up refusal without waiting on the store thread
