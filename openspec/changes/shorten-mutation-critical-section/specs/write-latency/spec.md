## ADDED Requirements

### Requirement: Bounded Mutation-Boundary Hold For Governed Semantic Writes

For a command on the narrowed-boundary set, the vault mutation boundary
SHALL be held only across the commit seam of a governed semantic write —
canonical write, index/log updates, and fencing — not across corpus
validation, relation-review evaluation, or embedding-model loading. The
worst-case in-boundary time SHALL be bounded by one census check plus, on a
census mismatch, one warm delta-reconciled revalidation; it MUST NOT include
a cold embedding-model load.

#### Scenario: A slow validator does not extend the held boundary

- **WHEN** pre-commit corpus validation for a narrowed command takes several
  seconds
- **THEN** that time is spent before the mutation boundary is acquired
- **AND** the measured boundary hold for the commit does not include it

#### Scenario: A cold embedding-model load never happens inside the boundary

- **WHEN** the embedding model has not yet been loaded in this process and a
  narrowed command's commit path would otherwise trigger a cold load
- **THEN** the load is attempted, best-effort, before the boundary is
  acquired
- **AND** the mutation snapshot observed at the moment of that load attempt
  reports no in-flight mutation for this write

### Requirement: Validity-Stamp Admission Governs Pre-Boundary Reuse

A pre-boundary preflight result SHALL carry a validity stamp composed of:
the corpus census the preflight's own corpus build already walked (never a
second stat-walk — the CI write-latency gate holds preflight to one walk
total), a scandir-census of the relation-review artifact and lifecycle
sidecar directories, and the boundary commit-generation read at preflight
ENTRY. The commit-generation is a monotonic per-vault counter advanced on
every mutation-guard exit while the boundary is still held, so every
governed writer moves it. Inside the boundary, admission SHALL compare only
the commit-generation and a fresh sidecar census against the stamp — an
O(sidecars) check with no corpus stat-walk; the pre-boundary corpus build,
evaluation, and validation results MAY be reused only when both match.
Identity, artifact-reservation, and destination-occupancy checks MUST always
be re-executed against live filesystem state inside the boundary regardless
of stamp match. A stamp mismatch, or a stamp that is `None` because an input
could not be censused cheaply or the commit-generation could not be read
(fail closed), MUST fall back to a full, bounded in-boundary revalidation
rather than reusing any pre-boundary result, and MUST produce the same
semantic-contract verdict a fresh evaluation would produce. Edits made
outside any governed writer (external tools, sync) are outside the stamp's
detection — for those, the same write-time protections the wide boundary
relied on (path guards, `create_only`, target freshness) still apply.

#### Scenario: A current stamp reuses pre-boundary validation

- **WHEN** the commit-generation inside the boundary equals the one captured
  at preflight entry and the fresh sidecar census matches the stamp
- **THEN** the commit reuses the pre-boundary corpus build, evaluation, and
  validation results instead of rebuilding them, without a corpus stat-walk
- **AND** the identity, artifact-reservation, and destination-occupancy
  checks still run fresh against live filesystem state

#### Scenario: A governed commit between preflight and commit invalidates reuse

- **WHEN** any governed writer commits (exiting a mutation guard) between
  the pre-boundary preflight and the in-boundary admission check
- **THEN** the commit-generation no longer matches the captured stamp
- **AND** the commit falls back to a full in-boundary revalidation that
  produces the same verdict a fresh evaluation would have produced, rather
  than reusing stale results

#### Scenario: An uncensusable tree or unreadable counter never reuses

- **WHEN** the corpus tree or the relation-review artifact and lifecycle
  sidecar directories cannot be censused cheaply (for example an unsafe
  symlink or reparse point), or the boundary commit-generation cannot be
  read for any reason other than never having been written
- **THEN** the validity stamp is `None`
- **AND** the commit always performs a full in-boundary revalidation, never
  a reuse

### Requirement: Revalidation-Outcome Telemetry

Every commit for a narrowed-boundary command SHALL record whether its
pre-boundary validation was reused, revalidated on a census mismatch, or
unavailable (no pre-boundary result to compare), as a counter labeled by
outcome and an accompanying log event. This telemetry, together with the
existing `exomem_boundary_hold_ms` measurement and the mutation journal's
hold fields, is the mechanism for observing whether the narrowed boundary
achieves its latency goal in production.

#### Scenario: A reused commit is counted separately from a revalidated one

- **WHEN** a commit reuses pre-boundary validation under an exact census
  match
- **THEN** `exomem_prevalidated_commit_total{outcome="reused"}` increments
- **AND** a commit that instead falls back to in-boundary revalidation
  increments `exomem_prevalidated_commit_total{outcome="revalidated"}`
  instead

### Requirement: Episode Derived Work Does Not Hold the Canonical Boundary

Episode recording SHALL serialize visibility admission, revision selection,
canonical Source and auxiliary writes, supersession and ledger binding. Derived
index fanout and episode housekeeping SHALL execute after that command guard
exits. Durable repair demand MUST exist before releasing custody, including
when fanout fails or the process exits after its canonical commit. Existing
receipt-owned fast acknowledgement MUST NOT receive duplicate fanout.

Managed `record` calls SHALL acquire raw reference owners before the canonical
boundary, using vault-wide source selection and the whole normalized batch.
The guarded record SHALL require exact root and identifier coverage, fresh
pending custody, caller-filtered uncertainty, policy and uniqueness admission.
It SHALL verify its preparation seal after acquisition, inside the guard and
after admission, immediately before revision selection. The seal SHALL bind
the active manager's readable commit generation, full freshness checkpoint,
live state and external-pending epoch. Borrowed snapshots SHALL also retain
current snapshot identity and compatible scan membership. Fresh non-live scans
remain valid preparation; they do not establish a current cached corpus.

An invalidated preparation SHALL leave the guard before requesting admission or
preparing again. Eligible keyed captures SHALL share their existing absolute
capture allowance, FIFO ticket and waiter slot across preparation and physical
contention retries. The first invalidation SHALL acquire admission and retain it
until completion. Ineligible managed calls SHALL retain at most two attempts.
A nested ineligible invocation SHALL NOT inherit eligible preparation admission.

Additional retries SHALL preserve the delivery reserve and check the allowance
before and after queue admission, before another census and before another guard.
The allowance SHALL NOT promise to interrupt an individual census or impose
a hard wall-clock limit. An unreadable entry generation SHALL skip the census.
Exhaustion or declined admission SHALL return
`REFERENCE_PREPARATION_UNAVAILABLE` with `status: retryable`, `committed: false`
and cause `generation_unavailable` or `preparation_changed`, before any episode
write or ledger bind. Ownership invalidation SHALL NOT increment physical busy
metrics or restart the leaf through its public error handler.
Other episode actions and the explicit wide-boundary
override SHALL retain their command guards. Direct calls without a managed
request SHALL retain their current guard behavior.

#### Scenario: Cold reference owners permit another writer

- **WHEN** a managed record waits for a cold reference-owner scan
- **THEN** an unrelated canonical writer can commit
- **AND** that commit invalidates the earlier preparation before revision selection

#### Scenario: Admission observes a new duplicate

- **WHEN** a duplicate identity arrives during pending or policy admission
- **THEN** the final seal rejects the preparation before revision selection
- **AND** a fresh preparation omits an ambiguous about-reference

#### Scenario: Pending identity cannot be proven for an about-reference

- **WHEN** pending custody leaves the identity of a recap's about-references unprovable at admission
- **THEN** the recap commits without those references and returns a warning with `about_skipped`
- **AND** an edit or observation by reference still refuses with the warming outcome while its target stays unprovable

#### Scenario: Reference preparation cannot be validated

- **WHEN** the capture allowance, waiter admission, delivery reserve or an unreadable generation prevents a sealed preparation
- **THEN** the caller receives the structured retryable outcome
- **AND** no episode Source or input-ledger revision is written
- **AND** a later unchanged request with the same idempotency key can commit once

#### Scenario: Concurrent reference captures invalidate preparation repeatedly

- **WHEN** canonical commits invalidate an eligible capture's preparation more than once
- **AND** its shared capture allowance and waiter admission permit another attempt
- **THEN** the capture prepares fresh ownership and commits once
- **AND** completion releases its existing FIFO ticket and waiter slot

#### Scenario: Slow recap indexing does not exclude an unrelated writer

- **WHEN** an episode has committed and its post-terminal fanout is blocked
- **THEN** an unrelated writer can acquire the canonical mutation boundary
- **AND** a failed fanout leaves durable repair demand without a retryable recap failure

#### Scenario: Concurrent recaps preserve revision ordering

- **WHEN** two writers record the same episode key concurrently
- **THEN** revision selection and supersession remain serialized
- **AND** an identical retry creates no extra revision

### Requirement: Graph Resolver Preparation Does Not Hold the Canonical Boundary

An incremental graph drain SHALL build or wait for its recall resolver before
acquiring the canonical mutation boundary. It MUST verify the complete sampled
recall checkpoint is still current and live inside that boundary before deriving
rows, reject newly observed external events during preparation or publication, and retain the existing source-version and transactional publication proofs.

#### Scenario: Cold resolver preparation permits an interactive write

- **WHEN** a graph drain waits for cold resolver preparation
- **THEN** an interactive writer can acquire the canonical mutation boundary

#### Scenario: A writer invalidates the prepared resolver

- **WHEN** recall checkpoint state changes during resolver preparation
- **THEN** the drain does not use that resolver to publish graph rows or availability
- **AND** repair demand remains queued for a later attempt

### Requirement: Index Fan-Out Runs Outside The Creation Namespace

A governed batch committed while its writer holds a vault creation namespace
SHALL run its post-commit index fan-out after that namespace is released, on
the writer's own thread and context and still inside the writer's mutation
boundary. The lexical upsert MUST NOT be refused as a nested creation lock for
a batch the writer itself just committed, so an ordinary governed write MUST
NOT defer its lexical rows, mint a durable full-index receipt, or revoke
retrieval admission on that account. A fan-out failure after release SHALL
surface to the writer as it did inside the namespace, and MUST NOT mask an
error already raised by the locked body. The batch's index report SHALL still
reach the command's response.

#### Scenario: An existing-page write lands its lexical rows inline

- **WHEN** `observe_memory` or `edit_memory` commits a page under the
  semantic-creation namespace
- **THEN** the lexical component of its index report completes inline
- **AND** no lexical repair is scheduled and no full-index receipt is minted

#### Scenario: A body failure after commit still fans out the committed batch

- **WHEN** the locked body raises after its batch committed
- **THEN** the batch fans out as the namespace is released
- **AND** the body's own error is the one the writer receives


### Requirement: The Whole-Vault Identity Inventory Is Built Before The Boundary

A command that mutates the vault SHALL build a cold private-identity inventory
before it acquires the vault mutation boundary, single-flighted with any
build already running, so the whole-vault walk never runs while the boundary
is held. A failed pre-boundary build MUST NOT refuse the write; the write
proceeds and pays for the walk where it would have before. Index count
refreshes that run inside a commit SHALL NOT cost a filesystem `stat` per page.

#### Scenario: A cold inventory is not built by a write inside its boundary

- **WHEN** `episode_memory` or a `record_memory` update runs against a vault
  whose identity inventory has not been built in this process
- **THEN** the inventory is built while the mutation boundary is free
- **AND** the write commits without building it under the boundary

### Requirement: Keyed Writes Absorb Ordinary Boundary Contention

A keyed write (any write that carries an explicit or implicit idempotency key)
that finds the mutation boundary busy SHALL be waited out on the server rather
than refused, whichever command it is, for a bounded
time (default 40 s, `EXOMEM_CAPTURE_WAIT_SECONDS`, and never past the caller's
own request budget: the wait MUST end early enough that the last attempt,
the delivery reserve and one guard acquire timeout still fit inside it).
Waiting writes SHALL attempt in arrival order within one process, and a write
arriving while others wait SHALL join behind them. Waiting parks a shared
synchronous worker, so no more than half of the workers (at least one)
MAY wait at once (the waiter cap; four at the default eight); a write past
that cap is refused at once with `cause: capture_waiters_full`. The retry MUST run
under the same idempotency identity, so the write commits exactly once, and
MUST NOT re-run any attempt that observed a commit. A retry SHALL start only
while the request budget still fits the delivery reserve plus an attempt as long
as the last one. A write whose own preparation fetched caller-supplied temporary
artifact handles before it met the busy boundary MUST NOT be re-run on the
server, because a handle cannot be fetched twice; its busy reaches the caller,
who retries with fresh handles. A client MUST NOT receive
`MUTATION_BUSY` for a keyed write whose holder is still inside its allowance,
whose wait has not run out, and that arrived within the waiter cap; absorption
holds up to the cap and no further.

A refusal remains only for a holder already past its allowance, a wait that
ran out, a write past the waiter cap, or a write that fetched caller-supplied
handles. It SHALL keep `MUTATION_BUSY` with `committed: false` and add a
`cause` (`holder_overdue`, `capture_wait_exhausted` or
`capture_waiters_full`), the time waited, and a
remediation that says the write did not commit and may be retried with the same
idempotency key. The REST `Error` schema SHALL declare every field such a refusal
carries. `EXOMEM_CAPTURE_CONTENTION_ABSORB=0` restores the previous immediate
refusal. The `capture` in the setting names and causes is historical; they apply
to every keyed write.

#### Scenario: Concurrent captures during a held boundary all commit once

- **WHEN** no more captures than the waiter cap allows (`remember`,
  `episode_memory`) meet a boundary held by another write
- **AND** the holder releases within its allowance
- **THEN** every write commits exactly once
- **AND** no `MUTATION_BUSY` reaches any caller

#### Scenario: A guarded Records update meets a busy boundary

- **WHEN** a keyed `record_memory` update meets a boundary held by a holder
  inside its allowance
- **THEN** the update waits, commits once after the holder releases
- **AND** no `MUTATION_BUSY` reaches the caller

#### Scenario: A write that fetched caller handles is not re-run

- **WHEN** a keyed `preserve_artifacts` call meets a busy boundary after it
  fetched its handles
- **THEN** the caller receives `MUTATION_BUSY` with `committed: false`
- **AND** each handle was fetched once, so a retry with fresh handles stores the files

#### Scenario: An overdue holder is refused at once with a cause

- **WHEN** a keyed write finds a holder already past its allowance
- **THEN** it is refused without waiting further, with `cause: holder_overdue`
- **AND** nothing was committed

#### Scenario: An exhausted wait names its cause

- **WHEN** the boundary stays busy for the whole bounded wait
- **THEN** the write is refused with `cause: capture_wait_exhausted`
- **AND** nothing was committed

#### Scenario: Waiting writes leave workers free for reads

- **WHEN** as many writes as the waiter cap allows are waiting on a held boundary
- **THEN** a further write is refused at once with `cause: capture_waiters_full`
- **AND** a read served by the same worker pool completes without queueing

#### Scenario: A wait never outlives the request budget

- **WHEN** a keyed write waits on a busy boundary under a bound request budget
- **THEN** it is refused, with its cause, before the delivery reserve begins
- **AND** an attempt whose preparation is too slow to fit again is not started again

#### Scenario: Only refusals a client saw are counted

- **WHEN** a keyed write retries on the server and then commits
- **THEN** `exomem_mutation_busy_total` does not increase
- **AND** the one refusal that does reach a client increases it once

### Requirement: Availability Republication Prepares Outside Canonical Authority

A graph availability repair SHALL run full source parsing and resolver topology
proof outside the canonical mutation boundary. Under authority it SHALL replay
exact source-byte and membership seals and validate its captured graph epoch,
recall publication, configuration, live database/WAL identity and metadata.
An invalidated or unreadable preparation SHALL decline without publishing or
advancing graph-sync acknowledgement. The final seal MAY read source bytes and
enumerate memberships, but MUST NOT reparse pages or reconstruct topology.

#### Scenario: Slow availability preparation admits a canonical writer

- **WHEN** the source/topology proof is paused before acquiring authority
- **THEN** an unrelated canonical writer can acquire the mutation boundary
- **AND** its commit invalidates the older prepared availability publication

#### Scenario: A direct edit or derived-store change invalidates the seal

- **WHEN** source bytes, policy, registry configuration or the live database/WAL
  changes after source proof and before publication
- **THEN** the availability repair declines without publishing its prepared marker
- **AND** same-sized source edits with restored timestamps are checked by bytes

### Requirement: Post-Commit Graph Receipts Use Their Dispatch Identity

Graph handoff and index-completion checks SHALL validate the checkpoint named
by their own dispatch result rather than rereading a later canonical checkpoint.
Completed outcomes require covering acknowledgement; registered outcomes require
their exact flight or covering acknowledgement. Deferred acceptance SHALL retain
its durable claiming-code, affected-path and dispatch-generation coverage checks.
A missing registered/deferred identity MUST fail closed without attributing its
failure to a different writer. Public report fields remain unchanged, and graph
read availability MUST still require proof for the current canonical checkpoint.

#### Scenario: A later writer advances the epoch before receipt assembly

- **WHEN** writer A's graph result is complete, registered or durably deferred
- **AND** writer B advances the canonical graph checkpoint before A's final check
- **THEN** A's receipt is validated against A's dispatch checkpoint
- **AND** B's outstanding work does not falsely turn A into a missing handoff

#### Scenario: A claimed flight or durable receipt is absent

- **WHEN** the dispatch identity or its claimed flight is missing, or a deferred
  batch lacks generation-current receipts covering every affected graph path
- **THEN** the index report remains incomplete and durable batch repair is kept
- **AND** a newer global checkpoint cannot substitute for the missing evidence

### Requirement: Curation Proposal Preparation Does Not Hold the Canonical Boundary

A curation proposal (`maintain_memory` with `mode="curation"` and
`curation_action="propose"`) SHALL prepare its plan outside the canonical
mutation boundary. Its seal SHALL take the boundary itself and, under it, verify
every content, absence and registry binding of the prepared plan before it
writes the run store; a binding that changed since preparation MUST refuse the
seal with that binding's code and write nothing. Registry identities SHALL be
captured before preparation reads the vault. Apply, resume, compensation and
every other maintenance mode keep the wide boundary, and
`EXOMEM_WIDE_MUTATION_BOUNDARY` restores the wide boundary for proposals.

#### Scenario: An unrelated write commits while a proposal is being prepared

- **WHEN** a proposal for a page edit is paused after it prepared its step
- **AND** another governed write commits to an unrelated page
- **THEN** that write commits without `MUTATION_BUSY`
- **AND** the proposal is then sealed with the mutation boundary held

#### Scenario: A write to a bound page refuses the seal

- **WHEN** a proposal for a page edit is paused after it prepared its step
- **AND** another governed write changes the page that the plan binds
- **THEN** that write commits without `MUTATION_BUSY`
- **AND** the proposal fails with `CURATION_BINDING_STALE` and creates no run
