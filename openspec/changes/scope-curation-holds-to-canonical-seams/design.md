## Context

A curation run executes one step per `apply` or `resume` call. The run store records the step's prepared state, its witness and its receipt. The witness rides the leaf's own commit batch, so a crash after the leaf commit leaves a witness that the next resume turns into a `recovered-committed` receipt. A proposal already works the way this change extends: it prepares outside the boundary, then `seal_proposal` takes the boundary and checks every binding again before it seals.

## Decisions

### D1. One helper decides whether a seam takes the boundary

`_step_boundary` takes the vault mutation boundary, or joins it when the calling thread already holds it. A nested guard advances the commit generation a second time, so a seam under an outer hold takes no new guard. The seal, the compensation seal, approval, each run-store write and the settle hold use it.

### D2. A step runs scoped only when its leaf guards itself

`SELF_GUARDED_STEP_KINDS` is a closed protocol set. A kind joins only when its leaf takes the boundary for its commit and checks the binding that the seal recorded under that guard:

| Kind | Guard and recheck |
|---|---|
| `edit`, `observe-unit` | `commit_existing` takes the boundary and checks the sealed `expected_hash`. |
| `create-note` | `_commit_creation` takes the boundary; the destination write is create-only. |
| Records and Planning kinds | The Records writer, or the collection store's `_mutate`, takes the boundary and checks the sealed container hash and item version. |
| `move`, `unmove` | `move_file` holds the boundary for the whole move. The witness check compares the destination bytes with the sealed postcondition, and `unmove` checks the sealed source hash. |

`supersede` stays out: its leaf binds the old page as it reads it, not as sealed. The preservation kinds stay out: their leaves take no guard of their own. A pointer correction and every episode-bound step stay out, because they write through paths that rely on the outer hold, such as the episode journal.

### D3. A scoped step

1. Probe outside the boundary: load the plan, reconstruct the run, pick the next step, and look for a committed witness, a rename intent or a blocker. When a probe finds any of these, run the step again under one hold, as before.
2. Write the prepared state under its own short hold.
3. Run the leaf with no curation hold. The leaf takes the boundary for its commit.
4. Take one short hold, the settle hold. Check the witness and the live postcondition, then write the receipt and the state. A leaf failure records its failed attempt under the same hold.

When the settle hold is refused, nothing more is written. A leaf that committed leaves its witness, so the caller gets `committed: true` and the next resume records `recovered-committed`. A leaf that did not commit leaves the run at its prepared state, and the next resume runs the step again.

### D4. A per-run claim replaces the serialisation that the wide hold gave

Two calls on one run would otherwise both pass the probes and both run the leaf. `_run_claim` admits one thread per run in a process and never waits, so a caller that holds the boundary cannot deadlock on it. A refused caller gets `CURATION_RUN_BUSY`, retryable, with nothing written. The claim covers `apply`, `resume`, `apply-compensation`, the compensation proposal and an episode leaf's invocation. The compensation proposal takes it because a forward step between its leaf commit and its receipt would otherwise be missing from the undo. An episode leaf takes it before it marks its attempt, so a busy run marks no attempt and the episode reconciles from the receipt later.

The claim is per process. Writers in two processes that share a vault serialise at the boundary. The settle hold therefore checks that the run's receipts are unchanged since the step's probe. When another process recorded one, the witness that it committed create-only has kept this leaf's batch out, and the call reports `CURATION_RUN_BUSY`.

### D5. The dispatcher asks curation

`curation.guards_own_boundary(kwargs)` names the curation actions that hold the boundary themselves: `propose`, `propose-compensation`, `apply`, `resume` and `apply-compensation`. `writer_lease` narrows exactly those, as it does for `maintenance_jobs.is_job_write`. `revert` keeps the wide hold. `EXOMEM_WIDE_MUTATION_BOUNDARY` widens all of them again.

## Risks

- A note save that lands between a step's leaf commit and its settle hold can make the live postcondition fail. The run then follows the existing uncertain-outcome path (`CURATION_OUTCOME_UNCERTAIN`) or recovers from the witness. This window is new and accepted.
- A page that another writer changes during a step's unlocked preflight makes the leaf refuse with its own stale-binding code. The run records a failed attempt where the wide hold made that writer wait.
- The leaf's own commit hold is unchanged. It can still exceed one second on a large vault; the semantic commit seam is a separate change.
