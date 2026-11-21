## Context

Promotion can carry an incremental graph origin while its availability remains correctly withdrawn for queued residue.
Startup currently requires availability before activation starts the graph drain.
An unrelated governed write can also publish availability without discharging repair outside its own paths.
Structural proof, durable repair and readable publication therefore need separate completion conditions.

The standby has no normal watcher before promotion.
Its checkpoint token identifies the sidecar, rather than every canonical byte changed since the proof.
External marks are process-local observations; durable receipts and barriers survive a restart.
The replacement process must observe or reconstruct the scoped change before it can retire that observation.

## Decisions

### D1: Seal the carried proof before ingress

After acquiring ownership, promotion arms observation on the normal file-mode watcher before replaying the held source guards.
Observation, seeding and dispatch become separate lifecycle steps on the same watcher, event buffer and shutdown owner.
The standby before ownership starts no watcher publication.

Promotion replays `_ProofReads` guards, both memberships and policy identity even when the sidecar checkpoint token matches.
Moved tokens or sources use the existing seeded D4 proof within the same request budget.
The migration branch retains its stronger proof.
A missing observer, expired deadline, unusable proof or unexplained movement leaves both graph handoff and startup validation uncarried.
The fallback cannot certify a new graph publication from the old checkpoint token.
Canonical writes queue their graph work while the fallback proof or repair establishes a valid source origin.
No normal writer advances graph acknowledgement or availability through that unproved interval.
The fallback retains source proof rather than merely postponing watcher validation after write admission.

The final decision uses a short canonical hold to bind the sidecar, durable epoch, policy and observation state.
Corpus work stays outside that hold.
Buffered events remain pending through normal seeding and catch-up.
A seed generation or stat-keyed cache hit cannot certify the source proof.
The existing read-proof memo and resolver must bind to the exact proved live checkpoint before callers reuse them.
Only a completed bridge adds `startup_validation` to the carried components.

A clean carry avoids repeating the source proof in watcher startup.
A carry with residue first records exact durable receipts and persists the read barrier.
Startup accepts that state as structurally proved with repair pending, so activation can start the ordinary drain.
A governed publication must preserve repair outside its batch.
It either repairs that debt or queues its own exact paths without advancing an acknowledgement it did not project.
Public readability still requires the ordinary marker, acknowledgement, policy and pending-event checks.

### D2: Contention is pending, not corruption

Startup distinguishes admissible, pending and a settled negative.
After a negative or ambiguous optimistic check, a stop-aware `try_hold` loop samples the bounded durable state under actual exclusion.
A lock miss, SQLite busy result or renewed holder keeps the result pending.
A governed write exceeding the former retry budget does not justify suspension or rebuild.
Shutdown cancels the wait; a lifecycle deadline leaves repair pending.

Cold source proof runs outside the hold.
A final short comparison must bind its negative evidence before suspension.
Only stable incoherence observed with the boundary free can take that branch.
The drain uses a strict result for its own epoch admission without changing the shared full-rebuild sampler's compatibility behavior.
Its current attempt supersedes stale scheduling observations only when durable receipts cover the scoped debt and no full marker stands.
Cold, unscoped, uncovered, incompatible and genuinely stalled work retains its recovery path.

### D3: The drain retires only committed, exact repair

The drain snapshots selected receipt revisions and each selected path's observed epoch.
A guarded observed-path input follows the existing corpus and freshness publishers.
It records touched paths even when their stat tuples are unchanged.
Guarded bytes, absence and current admission determine the publication; the raw mark supplies scope, not truth.

The existing owners patch or withdraw affected semantic, resolver, inbound and lexical consumers.
Named parse-cache entries are invalidated before replay.
Resolver admission includes the exact checkpoint generation and the proved source entries.
A consumer that cannot attest current state stays withdrawn through its existing repair owner.
Failed or ambiguous publication keeps the observation pending.
Potentially whole-corpus preparation stays outside the final canonical hold.

The graph derives rows through its existing bounded drain and dependency widening.
Each result distinguishes a derived page, proved deletion or exclusion, and failure.
Unreadable content, decoding failure, movement and an omitted result prove no deletion.
The final check binds source witnesses, resolver state, policy, topology and observation epochs.

Partial passes commit their rows with one bounded coverage record in existing `graph_meta` and retain the durable read barrier.
That record names the exact canonical checkpoint digest and its successfully committed required paths, with their source or absence witnesses.
It also binds the policy and topology proof that justified those rows.
Path names alone never certify later source state.
Its size follows the checkpoint's existing path cap; it is not an unbounded history.
Missing or mismatched coverage proves nothing.
A moved checkpoint restores the record's represented debt to the durable queue before the record is reset or rebound under proved lineage.
A mismatch cannot erase repair or supply acknowledgement coverage.
Full replacement and completed publication retire the obsolete record.
This closes checkpoints larger than the drain's selected-path limit without requiring one large boundary hold.

After restart, watcher startup recognizes matching partial coverage before its unconditional rebuild branch.
It reuses the existing cold source and topology proof to validate the sidecar and discover bounded residue.
That proof rechecks previously covered paths and paths outside the checkpoint; an offline edit can invalidate either.
Moved covered paths and newly proved residue return to the durable queue before startup hands work to the ordinary drain.
The barrier remains until bounded completion establishes the final proof.
A corpus-sized cold verification is allowed; it is not a whole-vault derivation or rebuild.
Missing authority or unbounded residue retains the established cold recovery fallback.

A final complete pass commits rows, exact acknowledgement and availability atomically.
It requires durable lineage coverage and no unproved outside, newer or unscoped observation that can change eligible graph rows or resolution.
Existing quarantine and held-read behavior remains unchanged.
A quarantined page alone creates no new vault-wide gate; existing warnings and reader policy remain in force.
After commit, the same canonical hold retires only proved `(path, epoch)` observations.
Durable receipts keep their existing revision compare-and-swap.
A failed transaction clears neither observations nor receipts.
A crash before retirement leaves replayable work and the required fence.
The scheduler attempts bounded completion for covered debt before whole-vault barrier recovery.
The existing availability republisher remains an empty-debt repair and never supplies a missing acknowledgement.

### D4: Re-prove a moved snapshot

- D4: A snapshot published after the standby's proof is re-proved inside the promotion request, not carried as is and not left to the promoted worker's warm. The old worker has exited, so nothing publishes during the re-proof, and the standby's proof kept the page reads it ended on. The re-proof starts from those reads and their memberships, rechecks every page's guard, re-reads only the pages that moved where the stat generation binds a page's bytes (every page on Windows), and evaluates the residue against the new snapshot, so the invariant is the standby's own. Carrying the old proof with the new snapshot's delta costs a whole-vault rebuild: the first write takes its delta from the moved checkpoint, declines an incomplete recall delta and rebuilds the whole vault. Admitting writes while the full handoff runs would need the graph queue to accept writes before any snapshot is adopted, a larger change to the write path. The re-proof is bounded at 40% of the budget the supervisor sends with the promotion request, so a slow re-proof declines `proof_deadline_passed` and falls back to today's full handoff instead of timing out the request and discarding the standby. The bound is sized from a measurement, not from a fixed share of the old ten-second cap: on a synthetic 13,000-page vault on a host at load 8, the seeded re-proof of one moved page took 2.2 to 2.6 s once each round walked the vault once for both memberships, against 3.1 to 3.5 s with a second walk of the KB, and a host at load 20 made it about 2.4 times slower. The supervisor's cap is therefore 30 s and the re-proof's bound 12 s. Requests wait at the supervisor meanwhile, which costs less than refusing writes for a 100-second handoff. The supervisor runs from the launcher venv, so one that predates the budget field sends none; the standby then assumes that supervisor's ten-second cap and bounds the re-proof at 4 s. A background re-proof still running when promotion starts stops at its next page, so it does not compete with the promotion's re-proof. The catalogue adoption that follows waits for the publication barrier only until 2 s before the request's cap; a busier barrier falls back to the repair after promotion, so the request ends inside its cap whatever holds the barrier.

## Reuse and cost

The implementation reuses the normal watcher, D4 source guards, mutation coordinator, existing publication owners and graph transaction.
The gaps require two small durable mechanisms: observed-path publication and checkpoint-scoped partial coverage.
They add one explicit publisher input and one bounded metadata encoding with reset rules.
No new queue, endpoint, storage service or external dependency is needed.
The observer remains owned by the normal runtime through failure, cancellation and shutdown.

The guard replay is O(paths and memberships), with byte reads where platform guards cannot bind content.
The final comparison is bounded metadata work; it does not make the whole replay O(1).
Observer arming and replay consume the existing 12-second proof allowance, or four seconds for a legacy request.
The containing request retains its existing 30-second or legacy ten-second cap.
A proof that cannot finish uses the established uncarried startup path.

Measure the new same-token path on the existing 13,000-page fixture.
Report guarded paths, bytes read, boundary hold time and promotion-to-admitted-graph on the same before and after instrument.
The drain selects at most 64 paths per batch, plus existing topology dependants.
High-fanout topology and resolver preparation can still grow with the corpus and must be reported separately.
The file-mode and rollback contracts remain in scope; store mode owns graph rows through its separate transaction.

## Risks and proof

A missed observer event or stale resolver must never become a positive carried proof.
The bridge relies on the existing live observation contract after its sealed interval.
The uncarried fallback pays the existing startup cost when observation or proof cannot complete.
A partial repair must retain its fence and exact coverage through failure and restart.
Older readers may ignore the optional progress key, but they must still respect the existing barrier and acknowledgement.

Test a silent edit between standby proof and cutover, followed by an unrelated write before the first read.
Test a current promotion with queued residue and cold startup.
After missing observation or an expired proof budget, test an unrelated first write before fallback validation.
Assert canonical durability without a graph publication that bypasses the fallback proof.
Test a real governed holder beyond the old retry budget, cancellation and free-boundary incoherence.
Test same-stat retargeting with warm consumers, explicit deletion, failed publication and newer marks.
Restart after a partial batch with an offline edit to a covered path and another outside the checkpoint.
Verify cold source proof, requeued residue and bounded completion without whole-vault derivation.
Preserve existing quarantine availability and warnings.
Preserve cold, unscoped, uncovered and schema-recovery cases.

## Delivery and closure

One coherent implementation batch receives independent review of its exact content and the required PR checks.
The review names stored-state, source-proof, scale and performance findings.
The ordinary publisher carries the merged batch through its existing release procedure.
The live installer retains its current authority and managed-upgrade procedure; this plan grants no new live access.
Rollback uses the maintained release rollback, without deleting state by hand.

Record the controlled promotion-to-admitted-graph result with a governed write during startup and zero unjustified whole-vault registrations.
Run the installed-wheel product restart step ten times in isolated state and verify results in the server process.
Do not infer server observations from the runner's process-local freshness maps.
Synchronize and archive only after the review, release and required installed acceptance evidence exist.
