## ADDED Requirements

### Requirement: Contended Writes Are Accepted Into A Durable Intent Journal

The system SHALL NOT refuse a write that is eligible for the intent journal because the vault mutation boundary is held, the waiting line or its worker slots are full, the holder is overdue, or a write-gating component is warming. It SHALL record the write as an intent in the vault's intent journal and answer it by the accept deadline.

The accept deadline SHALL be 2 seconds after the request arrives, or the request budget less its delivery reserve when that comes first. Every wait before the answer SHALL end at the accept deadline: the boundary acquire, the warm-up wait, the wait on an identical in-flight retry, the collection-store admission, and the wait for the intent to commit. Preflight work that the synchronous path also does, such as validation and file fetches, is not waiting. The wait after the intent is recorded SHALL be zero when no waiter slot is free or when a remote write carries no retry identity. When the intent commits before the accept deadline, the response SHALL be the committed terminal that the synchronous path returns. Otherwise the response SHALL be the accepted envelope with the intent's `intent_id` and reference.

An intent is non-terminal only while its state is `accepted` or `applying`. An eligible write SHALL first run inline, exactly as an ineligible write does, when its vault has no non-terminal intent and its caller has none. Before the attempt of an eligible write acquires the boundary, and again once it holds the boundary, the system SHALL check the journal for non-terminal intents. When one exists, the attempt SHALL stop before any effect, and the system SHALL record the write behind the existing intents. This check SHALL NOT apply to the applier, maintenance jobs, background holders, upload commits or ineligible commands.

A capture with files SHALL run inline first. When the commit of its first file meets a pre-commit refusal, the system SHALL record the capture as one intent. It SHALL move the bytes that the capture already fetched into the journal's private input store. The system SHALL NOT fetch those files again.

#### Scenario: A held boundary releases within the accept wait

- **WHEN** an eligible write meets a boundary that another process holds
- **AND** the holder releases before the accept deadline
- **THEN** the caller receives the committed terminal that an uncontended write returns
- **AND** no `MUTATION_BUSY` reaches the caller

#### Scenario: A held boundary outlasts the accept wait

- **WHEN** an eligible write meets a boundary that stays held past the accept deadline
- **THEN** the caller receives `state="accepted"`, `terminal=false`, `committed=false`, an `intent_id` and an intent reference
- **AND** the request waited at most 2 seconds in total
- **AND** the vault is unchanged until the applier commits the intent

#### Scenario: A write arrives behind queued intents

- **WHEN** an eligible write arrives while the vault's journal holds non-terminal intents
- **THEN** the system records the write behind them
- **AND** the write does not commit before them

#### Scenario: No waiter slot is free

- **WHEN** an eligible write meets a held boundary and every waiter slot is taken
- **THEN** the caller receives the accepted envelope at once instead of a refusal

#### Scenario: Maintenance runs while intents wait

- **WHEN** a maintenance job or an ineligible command needs the boundary while the journal holds non-terminal intents
- **THEN** it takes the boundary as it does today
- **AND** it receives no refusal caused by the queued intents

#### Scenario: An uncontended capture runs inline

- **WHEN** a capture with files finds a free boundary and no non-terminal intent
- **THEN** it commits inline and returns the committed terminal as it does today
- **AND** the journal holds no intent for it

#### Scenario: A capture meets a held boundary at its first file

- **WHEN** the commit of the first file of a capture meets a held boundary past the accept deadline
- **THEN** the caller receives the accepted envelope for the whole capture
- **AND** the applier commits every file once without fetching any file again

### Requirement: Accept Runs Every Check The Caller Controls

Before it records an intent, the system SHALL run these checks: authorization; argument coercion and size limits; the read-only-cell and reserved-path checks; the idempotency replay lookup; the vocabulary-application identity gate; the fetch and digest checks of open-world inputs; and the command's own semantic and governance validation. A failed check SHALL return the error that the synchronous path returns, and the system SHALL record no intent.

The system MAY accept with `validation="deferred"` only in four cases:

- validation needs a component that is still warming at the accept deadline;
- the caller's principal audience already has a non-terminal intent;
- a non-terminal intent already targets the same page;
- the command has no validation preview and met the boundary before its leaf.

In the second and third cases, the system SHALL record the write before its command leaf runs, because the write can depend on content that the earlier intent creates. An intent with deferred validation that fails validation at apply SHALL end `rejected` with the error that the synchronous path returns. An intent with complete validation SHALL end other than committed only through a change to the vault after accept, a conflicting use of its retry identity, or an explicit decision.

#### Scenario: An invalid draft is refused at accept

- **WHEN** an eligible write fails its semantic validation while the boundary is held
- **THEN** the caller receives the validation error synchronously
- **AND** the journal holds no intent for it

#### Scenario: A second edit follows an accepted edit of the same page

- **WHEN** an edit arrives for a page that a non-terminal intent already targets
- **THEN** the system accepts the edit with `validation="deferred"`
- **AND** the applier validates it against the page after the earlier intent applies

#### Scenario: A write depends on the caller's accepted write

- **WHEN** a caller's creation of page A was accepted and not applied
- **AND** the caller then creates page B with a link to page A
- **THEN** the system accepts page B with `validation="deferred"` instead of refusing the link
- **AND** the applier commits page A and then page B

### Requirement: The Intent Journal Is Durable, Machine-Local And Bounded

Each vault SHALL have one intent journal in its machine-local state directory, with private file permissions. The journal SHALL NOT be in the synced vault. The journal SHALL record its format version, and an applier SHALL NOT apply intents from a newer format than it knows. An intent, with any staged input bytes, SHALL be durable before the system answers with it. An intent SHALL hold:

- the canonical command and arguments, with staged input references in place of fetch handles;
- a principal snapshot without any credential, and the surface;
- the retry identity;
- the caller-bound basis guards, the target references and the content hash of each planned target at accept;
- the planned paths.

The applier SHALL apply intents in acceptance order. The journal SHALL bound non-terminal and conflicted intents by count and by bytes per vault. It SHALL limit one principal audience to a share of each bound. It SHALL keep a free-space floor on the state volume. The byte bound SHALL hold at least two of the largest inputs that one synchronous write accepts. The journal therefore accepts any single write that the synchronous path accepts.

The only refusal of an eligible write SHALL be `MUTATION_BUSY` with `status="retryable"`, `committed=false` and `retry_after_ms`. Its `cause` SHALL be `journal_full` when a bound or the free-space floor is reached. It SHALL be `journal_unavailable` when the journal cannot record the intent durably, cannot read its format, or no applier can run. A terminal intent SHALL drop its arguments and inputs. It SHALL keep its content-free fields and its compact terminal for the replay window. The applier SHALL delete staged inputs that no non-terminal or conflicted intent references.

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

#### Scenario: Staged inputs outlive their intent

- **WHEN** the process stops after it staged the inputs of a capture and before the intent row committed
- **THEN** the applier deletes those inputs at its next start

### Requirement: One Applier Per Vault Commits Each Intent Once

At most one applier per vault SHALL apply intents. An operating-system lock, which its process releases at exit, SHALL choose that applier. A serving process SHALL be the preferred applier.

The applier SHALL dispatch each intent through the shared command entry point, so every admission check, the command leaf and the postfilter run again. It SHALL dispatch as the principal that the intent recorded, with every field of that principal and without administrative ingress. Before each dispatch, it SHALL check that the caller's origin login is still active.

The retry identity of an intent SHALL be one key. The intent SHALL record that key, the digest of the original arguments, the replay window and a dispatch request identity. At most one non-terminal intent SHALL hold a key. The system SHALL record the intent before the inline attempt releases its claim on the key. The applier SHALL claim the key with the original digest. Arguments that the applier rewrites SHALL reach only the command leaf. A write without a retry identity SHALL receive a server-minted key that only its applier uses. The receipt of a key that an intent holds SHALL remain until the intent's retention ends.

The applier SHALL wait for the boundary without holding a request worker, in bounded attempts. It SHALL signal its wait to background holders and SHALL release the boundary between intents. After a crash, the applier SHALL resume non-terminal intents in order. When the applier cannot prove or exclude the commit of an intent, the intent SHALL become a write-conflict review item with cause `outcome_unknown` and the evidence for a decision. The applier SHALL NOT run that intent again automatically. On a graceful stop, the applier SHALL either finish the current intent or stop it before its boundary acquire.

#### Scenario: Two processes serve one vault

- **WHEN** two processes on one host each have intents for the same vault
- **THEN** one applier applies all of them in acceptance order

#### Scenario: The applier stops after the canonical commit

- **WHEN** the applier process is killed after the canonical commit of an intent and before the intent records its terminal
- **THEN** the next applier replays or resumes the terminal from the receipt of the intent
- **AND** the vault holds exactly one copy of the change

#### Scenario: The commit cannot be proven

- **WHEN** recovery finds an intent whose receipt reports the outcome as unknown, or an applying intent without a receipt
- **THEN** the intent becomes a write-conflict review item with cause `outcome_unknown`
- **AND** the item shows each planned target's content hash at accept and now
- **AND** the applier does not run it again until an explicit `retry-write` decision

#### Scenario: A resend finds its accepted intent

- **WHEN** a caller resends a write with the same retry identity while its intent is not terminal
- **THEN** it receives the current state and `intent_id` of that intent
- **AND** the system records no second intent

#### Scenario: A resend arrives after the intent ended

- **WHEN** a caller resends a write with the same retry identity within its replay window after its intent committed
- **THEN** it receives the committed terminal of that intent
- **AND** the command leaf does not run again

#### Scenario: The applier rewrites a moved target

- **WHEN** the applier rewrites the target of an intent because the page moved
- **THEN** it claims the retry identity with the digest of the original arguments
- **AND** a resend with the original arguments returns the intent, not `IDEMPOTENCY_KEY_REUSED`

#### Scenario: The caller's login was revoked after accept

- **WHEN** the origin login of an accepted intent is revoked before the applier dispatches it
- **THEN** the intent becomes a write-conflict review item with cause `authorization_revoked`
- **AND** the vault is unchanged

#### Scenario: The service stops gracefully during an apply

- **WHEN** the service stops while the applier waits for the boundary for an intent
- **THEN** the intent returns to `accepted`
- **AND** the next applier commits it once, with no review item

### Requirement: The Applier Respects Writer Authority

In a multi-host deployment, only the writer-lease holder SHALL apply intents. A replica with non-terminal intents SHALL NOT release the writer lease for idleness, and preferred reclaim SHALL count those intents as activity. An apply that loses its fencing token SHALL roll back through the existing transaction and SHALL return the intent to the line. The system SHALL NOT move an intent to another machine or replica.

#### Scenario: The lease is fenced during an apply

- **WHEN** the writer's fencing token changes while the applier commits an intent
- **THEN** the atomic write refuses the stale token and the transaction rolls back
- **AND** the intent returns to `accepted` and applies after the replica holds the lease again

#### Scenario: A replica with queued intents stays the writer

- **WHEN** a non-preferred replica has non-terminal intents and receives no request for longer than the idle-release time
- **THEN** it keeps the writer lease

### Requirement: Accepted Writes Reconcile On The Current Vault

The applier SHALL run each intent against the current vault. It SHALL plan navigation files again on every attempt. It SHALL apply a content-relative edit to current content. It SHALL follow a moved target through the stable reference recorded at accept when that reference resolves to one current path.

The caller's own basis can stop holding in five ways: a caller-bound hash or token no longer matches, the target is deleted, the vocabulary binding changed, a review step is now required, or a contract now blocks the write. In each case the intent SHALL become a write-conflict review item with state `conflicted` and a closed cause code. The same SHALL happen in three more cases: the caller's authorization was revoked or expired; the retry identity was used for other input; or a cause that belongs to one intent outlasts the per-intent bound (`stalled`). A dispatch that reports a commit, including a committed-uncertain or committed-failure outcome, SHALL end the intent `committed` and SHALL NOT be retried. The system SHALL NOT overwrite current content or drop an intent silently.

A write-conflict item SHALL keep the intent's arguments readable by its principal and by the owner until a decision. A `retry-write` decision SHALL put the intent at the end of the line for a fresh attempt under a new retry identity. Only the owner MAY retry an `outcome_unknown` item. A `retry-write` decision SHALL NOT run an intent whose authorization was revoked or expired. A `discard-write` decision SHALL end it `discarded` with a recorded reason. A discard or a rejection SHALL release any vocabulary application binding that the intent holds.

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

#### Scenario: One intent's cause outlasts its bound

- **WHEN** an intent waits for a reference that never prepares, for longer than the per-intent bound
- **THEN** the intent becomes a write-conflict review item with cause `stalled`
- **AND** the later intents in the line apply

#### Scenario: The owner retries an unknown outcome

- **WHEN** the owner triages an `outcome_unknown` item with `retry-write`
- **THEN** the applier runs the intent under a new retry identity
- **AND** the original retry identity keeps its unknown outcome

### Requirement: Callers Observe Accepted Writes By Intent Reference

`review_memory(mode="write-intent", ref=...)` SHALL return the current state of an intent. Once the intent is terminal, it SHALL also return the compact terminal, projected for the current authority of the reader. It MAY wait up to `wait_seconds`, within the request budget less its delivery reserve. A wait SHALL take a waiter slot from the same cap as other request waits, and SHALL return the current state at once when no slot is free. Without `ref`, it SHALL list the reader's own non-terminal and write-conflict intents, with a bound. A reader SHALL see only intents of its own principal audience, except the owner, who sees all intents. The recall and bootstrap status SHALL report the reader's counts of accepted and conflicted writes. The attention review SHALL report the count of undecided write conflicts.

#### Scenario: A caller polls until commit

- **WHEN** a caller polls an accepted intent with `wait_seconds`, and the intent commits during the wait
- **THEN** the poll returns `state="committed"` with the same compact terminal that an uncontended write returns

#### Scenario: Another audience polls an intent

- **WHEN** a principal polls an intent that a different non-owner audience accepted
- **THEN** the response is the same as for an unknown reference

#### Scenario: A poll finds no waiter slot

- **WHEN** a caller polls with `wait_seconds` while every waiter slot is taken
- **THEN** the poll returns the current state of the intent at once

### Requirement: Reads Disclose The Reader's Own Accepted Writes

A read SHALL NOT present the content of an intent that has not committed. A direct read of a path that the reader's own non-terminal intent targets SHALL return the canonical bytes and name that intent in `pending_writes`. Recall, search and browse SHALL add the reader's own non-terminal intents to their status. After the intent commits, the existing pending recall overlay SHALL provide read-your-write. A read SHALL NOT fail or wait because the journal is unreadable; it SHALL then omit `pending_writes`.

#### Scenario: A read follows an accepted edit

- **WHEN** a caller reads a page right after its edit of that page was accepted and not applied
- **THEN** the read returns the current canonical bytes
- **AND** `pending_writes` names the caller's intent and its state

#### Scenario: A search follows the commit

- **WHEN** the applier commits the intent and the caller then searches for its new terms
- **THEN** keyword recall returns the new content through the pending recall overlay

### Requirement: Eligibility Is Declared Per Command

Each write command SHALL declare whether it is eligible for the intent journal, with a reason. The system SHALL NOT record as an intent a read, a validation preview, a maintenance job, a background boundary holder, a write-intent decision, or a command whose result carries a bearer credential. The system SHALL NOT replay a bearer credential.

#### Scenario: A transfer is prepared during a long capture

- **WHEN** a client prepares an upload transfer while a capture holds the vault boundary
- **THEN** the system records no intent for the transfer
- **AND** the transfer returns its capability or its synchronous error

### Requirement: Caller-Visible Write Outcomes Carry Their Cause

The caller-visible mutation journal SHALL record the `cause` of every `MUTATION_BUSY` refusal that reaches a caller. It SHALL NOT record the internal refusal that moves an attempt behind queued intents. It SHALL record one row for each intent outcome (`accepted`, `applied`, `rejected`, `conflicted`, `discarded`), linked by `intent_id`. A hosted cell SHALL record no vault content in these rows.

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

- **WHEN** an eligible write cannot acquire the vault boundary by the accept deadline
- **THEN** its attempt changes no vault or sidecar file
- **AND** the system records it as an intent and answers it as committed or accepted, not busy

#### Scenario: Lock backend cannot prove ownership

- **WHEN** the process-safe locking primitive is unavailable or returns an indeterminate ownership result
- **THEN** hosted mutation readiness fails closed
- **AND** no write is attempted under assumed ownership

### Requirement: Tenant-Scoped Retry And Idempotency Semantics

All MCP mutations, including hosted mutations, SHALL preserve caller-supplied idempotency keys and bounded implicit retry replay through the existing common invocation boundary. Retry identity MUST include the resolved vault or tenant, authenticated principal scope, command, and canonical arguments. A key or implicit retry from one tenant MUST NOT replay or suppress a mutation for another tenant. An identical pending retry MUST inspect or wait on its receipt outside the exclusive vault mutation boundary, while different identities remain subject to normal serialization. An identical retry of a write that the intent journal holds SHALL return the current state of that intent, or its terminal within the replay window, and SHALL NOT record a second intent. Failed precommit mutations MUST become retryable, and committed terminal outcomes MUST replay without executing the leaf again.

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

A keyed governed write that arrives while a write-gating warm-up component is still warming SHALL wait for that component inside its request, within the bounded wait and the parked-worker cap that a busy boundary uses, and SHALL then run once. A write that is eligible for the intent journal SHALL NOT receive the warm-up refusal, and its warm-up wait SHALL end at the accept deadline instead of the bounded wait. When the accept deadline passes first, when no wait slot is free, or when the write carries no key, the system SHALL accept it as an intent with `validation="deferred"`. The applier SHALL apply that intent only after the component is ready. For a write that is not eligible, the system SHALL return the retryable warm-up refusal only when that bound runs out first or no wait slot is free, and the refusal SHALL report how long the write waited. An ineligible unkeyed write SHALL keep the immediate retryable refusal, because a resend after a client gave up during the wait would run it twice. An ineligible write attempted on a collection store's thread SHALL keep it too, because that thread never parks.

#### Scenario: A keyed write during a short warm-up commits

- **WHEN** a keyed governed write arrives while the semantic corpus is warming
- **AND** the corpus becomes ready within the bounded wait
- **THEN** the write runs once and commits
- **AND** the client receives no warm-up refusal

#### Scenario: An eligible write outlasts the warm-up bound

- **WHEN** an eligible governed write arrives while a write-gating component stays warming past the accept deadline
- **THEN** the caller receives the accepted envelope with `validation="deferred"` by the accept deadline
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
