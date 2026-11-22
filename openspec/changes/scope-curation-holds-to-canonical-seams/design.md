## Context

A curation run executes one step per `apply` or `resume` call. The run store records the step's prepared state, its witness and its receipt. The witness rides the leaf's own commit batch, so a crash after the leaf commit leaves a witness that the next resume turns into a `recovered-committed` receipt. A proposal already works the way this change extends: it prepares outside the boundary, then `seal_proposal` takes the boundary and checks every binding again before it seals.

## Decisions

### D1. One helper decides whether a seam takes the boundary

`_step_boundary` takes the vault mutation boundary, or joins it when the calling thread already holds it. A nested guard advances the commit generation a second time, so a seam under an outer hold takes no new guard. The seal, the compensation seal, approval, each run-store write and the settle hold use it.

Each hold advances the commit generation once. A scoped step therefore advances it three times where the wide hold advanced it once: at the prepared-state write, at the leaf's commit and at the settle hold. `apply` adds one advance for its approval. Readers use the generation only as a validity stamp, so the extra advances cost them a revalidation.

### D2. A step runs scoped only when its leaf guards itself

`SELF_GUARDED_STEP_KINDS` is a closed protocol set. A kind joins only when its leaf takes the boundary for its commit and, under that guard, checks the sealed pre-step state of what it writes:

| Kind | Guard and recheck |
|---|---|
| `edit`, `observe-unit` | `commit_existing` takes the boundary and checks the sealed `expected_hash`. |
| `create-note` | `_commit_creation` takes the boundary; the destination write is create-only. |
| Records and Planning kinds | The Records writer, or the collection store's `_mutate`, takes the boundary and checks the sealed container hash and item version. |
| `move`, `unmove` | `move_file` holds the boundary for the whole move. In its before-rename stage, under that guard, `_dispatch_step` checks the source against its sealed hash and refuses with `SOURCE_CHANGED`. The destination bytes alone cannot show another write, because a `fields` patch sets its field whatever the page holds. |

`supersede` stays out: its leaf binds the old page as it reads it, not as sealed. The preservation kinds stay out: their leaves take no guard of their own. A pointer correction and every episode-bound step stay out, because they write through paths that rely on the outer hold, such as the episode journal.

### D3. A scoped step

1. Probe outside the boundary: load the plan, reconstruct the run, pick the next step, and look for a committed witness, a rename intent or a blocker. When a probe finds any of these, run the step again under one hold, as before.
2. Write the prepared state under its own short hold.
3. Run the leaf with no curation hold. The leaf takes the boundary for its commit.
4. Take one short hold, the settle hold. Check the witness and the live postcondition, then write the receipt and the state. A leaf failure records its failed attempt under the same hold.

A self-guarded leaf releases the boundary when its batch commits, so another writer may change the page that the step wrote before the settle hold. When this call saw its own leaf's batch commit the witness, the live check at the settle hold accepts such a change as later work: the step gets its receipt, and its undo reports `CURATION_BINDING_STALE`. Recovery from any other witness keeps the live check that it had under the wide hold, because a batch whose rollback could not remove the witness restored the pages without it. That check covers a leaf that raised, every later resume, the `status` action and the wide hold. An unsafe or unreadable path, and a Records receipt that does not corroborate, make the outcome uncertain in every case.

When the settle hold is refused, nothing more is written. A leaf that committed leaves its witness, so the caller gets `committed: true` and the next resume records `recovered-committed`. When another writer changes the step's page before that resume, the run blocks as uncertain, as a stop under the wide hold followed by such a change already did. A leaf that did not commit leaves the run at its prepared state, and the next resume runs the step again.

### D4. A per-run claim replaces the serialisation that the wide hold gave

Two calls on one run would otherwise both pass the probes and both run the leaf. `_run_claim` admits one thread per run in a process and never waits, so a caller that holds the boundary cannot deadlock on it. A refused caller gets `CURATION_RUN_BUSY`, retryable, with nothing written. The claim covers `apply`, `resume`, `apply-compensation`, the compensation proposal and an episode leaf's invocation. The compensation proposal takes it because a forward step between its leaf commit and its receipt would otherwise be missing from the undo. An episode leaf takes it before it marks its attempt, so a busy run marks no attempt and the episode reconciles from the receipt later.

The claim is per process. Writers in two processes that share a vault serialise at the boundary. The settle hold therefore checks that the run's receipts are unchanged since the step's probe. When another process recorded one, it has settled the step: either its create-only witness kept this leaf's batch out, or it recovered this leaf's witness. The call reports `CURATION_RUN_BUSY`, and that receipt accounts for the step.

### D5. The dispatcher asks curation

`curation.guards_own_boundary(kwargs)` names the curation actions that hold the boundary themselves: `propose`, `propose-compensation`, `apply`, `resume` and `apply-compensation`. `writer_lease` narrows exactly those, as it does for `maintenance_jobs.is_job_write`. `revert` keeps the wide hold. `EXOMEM_WIDE_MUTATION_BOUNDARY` widens all of them again.

## Risks

- A note save that lands between a step's leaf commit and its settle hold changes a page that the step wrote. The step still gets its receipt in that call, and its undo reports `CURATION_BINDING_STALE` for that page. When the call stops or its settle hold is refused first, the next resume blocks the run as uncertain. This window is new and accepted.
- A page that another writer changes during a step's unlocked preflight makes the leaf refuse with its own stale-binding code. The run records a failed attempt where the wide hold made that writer wait.
- The leaf's own commit hold is unchanged. It can still exceed one second on a large vault; the semantic commit seam is a separate change.
