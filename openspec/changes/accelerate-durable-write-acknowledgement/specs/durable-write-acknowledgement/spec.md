## Purpose

Make a governed write acknowledge canonical durability promptly without losing
exact custody, visibility, or honest status for the derived work that follows.

## ADDED Requirements

### Requirement: Acknowledgement Ends At Proven Canonical Durability And Derived Custody

A governed semantic write SHALL acknowledge success only after every semantic
and governance precondition has passed, mutation authority and fencing have
been proven, the complete canonical batch has committed, the canonical mutation
terminal is durable, and every required unfinished derived component has exact
durable custody. Derived graph, vector, claim, lexical-catalog, resolver, or
advisory completion MUST NOT be a precondition for acknowledging canonical
success when exact custody and the required read-visibility projection are
already proven. Deferral MUST NOT weaken validation, rollback, fencing,
idempotency, graph-epoch, or committed-uncertain behavior.

#### Scenario: Canonical write returns while derived work is pending

- **WHEN** a governed write commits its complete canonical batch and terminal
- **AND** exact durable receipts cover every unfinished derived component
- **THEN** the caller receives a committed terminal without waiting for those components to converge
- **AND** the response identifies the unfinished component state as pending

#### Scenario: Derived custody cannot be proven

- **WHEN** canonical bytes may have committed but the required derived receipt or exact terminal cannot be proven durable
- **THEN** the system preserves the existing committed-uncertain semantics
- **AND** it does not report an ordinary committed success or invite a new mutation identity

### Requirement: Prepared Derived Receipts Survive Every Canonical Crash Cut

Before the first canonical replacement, the system SHALL durably prepare one
closed, bounded derived-batch receipt that binds the mutation attempt, canonical
generation, affected safe relative paths, exact before and intended after hashes
or tombstones, and the required component set. The receipt MUST carry no
arbitrary vault content. A prepared receipt SHALL authorize derived publication
only after exact canonical state proves, for every path, either the intended
after-state or a later state that a newer exact receipt covers with live or
retired pending custody for that path, or a later state whose current bytes
both persistent recall lanes (the lexical catalogue and the reference sidecar)
already hold. Rollback,
partial state, or an unrelated later state MUST NOT activate it. The recorded
canonical generation is lineage for ordering and supersession; proof SHALL NOT
require it to equal the vault-wide checkpoint, which advances on every write to
any page. A later exact receipt MAY take over an older receipt's demand path
by path, only for a path it covers without a visibility gap; the older receipt
still converges the paths it owns, and is superseded as a whole only when newer
receipts cover every path/component demand. The same per-path predicate SHALL
hold at the acknowledgement's proof, at every re-proof before a component is
dispatched, and at component completion.

#### Scenario: Process dies after canonical replacement

- **WHEN** the process dies after canonical files commit but before any post-commit activation update
- **THEN** restart recovery proves the prepared receipt against the canonical after-hashes
- **AND** every required component remains eligible for exactly-once-publication or at-least-once safe replay without re-executing the canonical mutation

#### Scenario: Caught batch failure rolls back

- **WHEN** a caught failure restores the complete canonical before-state after a receipt was prepared
- **THEN** the receipt cannot authorize publication of the intended after-state
- **AND** cleanup may retire it without producing derived rows for uncommitted content

#### Scenario: Newer mutation supersedes pending work

- **WHEN** a later governed mutation changes a path before an older derived receipt completes
- **THEN** the older receipt cannot republish the stale generation
- **AND** it is retired only after newer exact custody or full reconciliation covers the path and component

#### Scenario: Distinct pages sharing navigation and cited pages converge

- **WHEN** several governed pages are each written once in a burst and every write also rewrites shared pages (the knowledge base log and index, a cited source's back-reference)
- **THEN** every batch completes or is superseded, and none is left in `reconcile_required`
- **AND** every pending-visibility row retires, and managed recall stays ready

#### Scenario: A shared page returned to its earlier bytes is handed on

- **WHEN** a newer write re-renders a shared page exactly as it was before an older batch's write
- **THEN** the older batch hands that page to the newer batch, whose proven after-state those bytes are
- **AND** it converges its own paths instead of being held in `reconcile_required`

#### Scenario: A coverer not yet proven heals on the next pass

- **WHEN** an older batch is re-proven after a newer committed write moved one of its shared paths but before the newer batch's own proof
- **THEN** the older batch is held in `reconcile_required`
- **AND** the next recovery pass proves the newer batch and then the older one, which converges its own paths

#### Scenario: An out-of-band edit heals once the recall lanes hold it

- **WHEN** a page an unconverged batch wrote is edited outside any governed write
- **THEN** the batch is held in `reconcile_required` while either recall lane lacks the edited bytes
- **AND** once both lanes hold them, recovery hands the page on and the batch converges its other paths, or is superseded when it owns none

#### Scenario: A new page deleted by hand before it converges heals

- **WHEN** a page a proven batch created is deleted outside any governed write before the batch converges
- **THEN** the batch is held while either recall lane still holds the page, and once both hold its absence recovery hands the page on and the batch converges its other paths, with no operator step
- **AND** managed recall is ready once the lanes hold the absence, and the page's advisory result is superseded

#### Scenario: Multi-page burst keeps every batch provable

- **WHEN** several governed pages are each written more than once in one burst
- **THEN** every older batch retires as `superseded` and every newest batch completes, with no batch left in `reconcile_required`
- **AND** every pending-visibility row of the superseded batches is retired

### Requirement: Post-Canonical Waiting Has One Shared Two-Second Budget

The interactive request SHALL have one shared, hard, non-configurable 2.0-second
budget for all optional completion after canonical durability. The budget starts
at the canonical-commit event and MUST NOT be reset or applied once per
component. If all required work is already settled, the request SHALL return
immediately without consuming the budget. Budget expiry alone SHALL produce a
typed pending outcome; a real registration, proof, or publication error MUST
remain a distinct error or failed component outcome.

#### Scenario: No derived work is outstanding

- **WHEN** canonical commit leaves every required component already current
- **THEN** the request returns immediately after terminal persistence
- **AND** it does not wait for the remainder of the two-second budget

#### Scenario: Several components remain slow

- **WHEN** graph, embeddings, and advisory work all remain unfinished after canonical commit
- **THEN** their combined interactive wait is bounded by one two-second budget
- **AND** the terminal reports their closed component states as pending

#### Scenario: Registration fails before the deadline

- **WHEN** exact durable custody for a required component fails inside the two-second window
- **THEN** the caller receives the corresponding real failure or committed-uncertain outcome
- **AND** the deadline does not convert that failure into pending

### Requirement: Default Write Advisories Run Outside The Acknowledgement Path

Default near-duplicate and overlap advisory computation SHALL be durable
background review work and SHALL NOT delay a compact committed terminal. It
MUST evaluate only the exact current canonical generation, persist any surfaced
advisory result behind a stable opaque result reference, and become superseded
when the target generation no longer matches. Exact lookup SHALL return one of
`pending`, `ready`, `failed`, or `superseded`; ready results SHALL carry the
bounded existing warning/review references that the triggering write would have
returned. Every applicable advisory job SHALL have a result reference whether it
is pending, ready, or failed at acknowledgement; an inapplicable job SHALL have
none. The result reference SHALL be stable across exact mutation replay and
remain resolvable at least as long as that mutation terminal is replayable.
Where background embedding produces the page vectors needed by the advisory,
the advisory SHALL reuse those vectors rather than encode the same generation
again. `suggestions=true` SHALL remain an explicit enriched synchronous opt-in
whose added latency is outside the default fast-acknowledgement guarantee.

For a route that sweeps inline, the deferred sweep SHALL compute exactly what
that route's inline sweep computes, through the same function over the same
inputs, and the route SHALL NOT also sweep inline when its committed batch holds
the advisory custody: `remember` scores the draft title and normalized body,
reuses the page's published vectors where the chunk text matches, flags
near-duplicates of the note's own type only, and flags overlaps, in the inline
tie order; `edit` scores the new body's bare paragraphs for overlaps only, and
an edit that leaves the body unchanged takes no advisory custody; a vault-scope
write reports the same counterparts, including pages outside the knowledge
base. The route's inputs are held in the committing process only; a component
that runs where they are unavailable falls back to its generic sweep over the
page's published vectors rather than leaving the job pending. `capture` keeps
its inline sweep and takes no advisory custody.

#### Scenario: Default compiled write needs an advisory sweep

- **WHEN** a default compact compiled write commits and its near-duplicate or overlap sweep is unfinished
- **THEN** the terminal reports `advisory_sync="pending"`, returns its stable result reference, and does not run the sweep inline
- **AND** exact lookup later returns ready, failed, or superseded rather than leaving a finished or failed job permanently pending

#### Scenario: A fast-acknowledged remember or edit returns its sweep by reference

- **WHEN** fast acknowledgement is active and a default `remember`, or an `edit` that changes the body, commits a batch holding the advisory custody
- **THEN** the write computes no inline duplicate or overlap sweep and returns none of its warnings
- **AND** the ready result reached through `advisory_result_ref` carries, in order, exactly the warnings the same write returns with fast acknowledgement off

#### Scenario: An edit that leaves the body unchanged has no advisory job

- **WHEN** fast acknowledgement is active and an edit changes only frontmatter
- **THEN** the terminal reports `advisory_sync="not_required"` and carries no result reference, as the inline sweep computes nothing for it

#### Scenario: Fast acknowledgement off leaves every inline sweep unchanged

- **WHEN** `EXOMEM_FAST_DURABLE_ACK` is not `1`
- **THEN** `remember`, `edit` and `capture` compute their inline sweeps and return their warnings exactly as before

#### Scenario: Exact retry replays the advisory reference

- **WHEN** the exact mutation identity is retried after its advisory job changes state
- **THEN** the original compact terminal and advisory result reference replay byte-for-byte
- **AND** exact result lookup independently reports the current job state

#### Scenario: Advisory completes before acknowledgement

- **WHEN** an applicable advisory job reaches its ready result before the compact terminal is persisted
- **THEN** the terminal reports `advisory_sync="completed"` and carries the same stable result reference
- **AND** exact lookup returns the ready result without recomputing it

#### Scenario: Explicit suggestions are requested

- **WHEN** the caller supplies `suggestions=true`
- **THEN** the enriched suggestion pass may run synchronously under its existing explicit opt-in contract
- **AND** the default fast-write latency claim does not silently include that optional spend

#### Scenario: Embedding and advisory share one generation

- **WHEN** one background pass encodes a page for both vector publication and advisory comparison
- **THEN** both consumers use that exact generation's vectors
- **AND** the advisory does not invoke a second encode of the same chunks

### Requirement: Stranded Derived Receipts Are Visible And Repairable

A derived batch held in `reconcile_required` SHALL be reported apart from
crash-cut recovery work: the store SHALL count stranded batches separately from
batches a drain pass will prove on its own, and `doctor` SHALL report, in one
content-free line, whether fast acknowledgement is active, the due component
count and its oldest age, the stranded and recovering batch counts, and the
pending-visibility outcome with its closed failure code. `doctor` SHALL fail
while any batch is stranded and SHALL NOT mutate custody to compute the line.
Operator reconciliation (`maintain --reconcile`) SHALL converge each stranded
batch's paths from their current canonical bytes through the ordinary writer
fan-out and SHALL retire the batch as `superseded`, with its pending rows,
once both recall lanes hold every path's current bytes; a batch the lanes still
lack SHALL stay stranded and be counted as remaining. Reconciliation SHALL
retire receipt custody only: a `ready` or `failed` advisory result SHALL stay
as published, and only an advisory result that never ran SHALL be settled -- as
`failed` with code `advisory_unavailable` when its target page is unchanged, or
`superseded` when the target moved. The reconcile report SHALL carry counts
only.

#### Scenario: Doctor names stranded custody

- **WHEN** a batch is held in `reconcile_required`
- **THEN** `doctor` fails its fast-acknowledgement custody check with a line that carries counts, one age and closed codes, and no path, title or identifier
- **AND** the remediation names `maintain --reconcile`

#### Scenario: Reconcile retires a stranded batch from current bytes

- **WHEN** an operator runs `maintain --reconcile` while a batch is stranded
- **THEN** the batch's pages are converged from their current bytes and the batch is retired once both recall lanes hold them
- **AND** managed recall is ready afterwards, and a dry run reports the same counts without changing custody

#### Scenario: A ready advisory survives reconcile

- **WHEN** a batch was stranded by a shared page while its own page stayed in its after-state, and its advisory result is `ready`
- **THEN** reconciliation retires the batch and leaves the advisory result `ready`, so the warnings it carries still resolve
- **AND** an advisory result for such a batch that never ran resolves as `failed` with code `advisory_unavailable` rather than `superseded`

### Requirement: Fast Acknowledgement Is Proven End To End

The performance gate SHALL measure the complete default product mutation from
public-leaf entry through returned acknowledgement, the canonical-commit to
response interval, and an immediate read after the write. It SHALL exercise
warm steady state, derived recovery, pending receipts, and cold or evicted
read-visibility state on realistic corpus sizes. A boundary-only or commit-only
measurement MUST NOT be accepted as proof of this capability. The deterministic
2,000-page and 8,000-page gates SHALL require warm public-write p95 below 4.0
seconds, immediate exact keyword/hybrid read p95 below 1.5 seconds, and their
paired write-plus-read p95 below 5.0 seconds. The canonical-commit-to-response
interval remains subject to the hard shared 2.0-second bound. A quiesced installed
product acceptance SHALL measure server and connector separately and require
default-write p50 at or below 3.0 seconds and p90 at or below 5.0 seconds.

#### Scenario: Work is merely moved after the measured boundary

- **WHEN** a change shortens the mutation-boundary hold but makes the public acknowledgement or immediate read slower
- **THEN** the end-to-end gate fails or reports the regression
- **AND** the change cannot claim fast durable acknowledgement from the boundary metric alone

#### Scenario: Recovery work is active

- **WHEN** the graph needs recovery or vector/advisory receipts are pending during a measured default write
- **THEN** the canonical-commit to response interval stays within the shared post-canonical bound
- **AND** the immediate read returns the committed generation or an explicit fail-closed warming outcome, never stale content
