## Why

A curation `apply`, `resume` or `apply-compensation` call holds the vault mutation boundary for its whole step. The step plans, probes, runs its leaf's preflight and then commits, and every other writer waits for all of it. A note save that arrives during a step waits out its bound and can be refused with `MUTATION_BUSY`.

On 2026-11-22 the personal service's mutation journal recorded 78 curation resumes in one morning. They held the boundary for 2,659 s in total, with a p90 of 110.8 s and a maximum of 690.9 s. 17 of 38 `MUTATION_BUSY` refusals of note saves fell inside such a hold. A measured resume spent 5% of its hold before its first commit, 26% inside commits, 62% between commits and 6% after.

Most of a step does not need the boundary. Its leaf already takes the boundary for its own commit and checks its sealed binding there.

## What Changes

- A curation step holds the boundary only at its canonical seams: the prepared-state write, the leaf's own commit, and one short hold that checks the witness and the live postcondition and records the receipt and state. Plan load, reconstruction, step selection, the witness and rename-intent probes, blockers and runtime binding run outside the boundary. A probe that finds anything to recover decides the step again under one hold.
- A step kind runs its leaf outside curation's hold only when its leaf takes the boundary itself and checks its sealed binding under it: `create-note`, `edit` (not a pointer correction), `observe-unit`, the Records and Planning kinds, `move` and `unmove`. `supersede`, the preservation kinds, a pointer correction and every episode-bound step keep the whole step under one hold.
- Approval checks the plan identity and every binding and records approval under one short hold.
- A call that finds the same run already executing in its process is refused at once with a retryable `CURATION_RUN_BUSY`, which commits nothing. A compensation proposal takes the same claim.
- The dispatcher no longer holds the wide boundary for `apply`, `resume` and `apply-compensation`. `EXOMEM_WIDE_MUTATION_BOUNDARY` restores the wide hold.

## Capabilities

### Modified Capabilities

- `hosted-mutation-safety`: a curation step holds the boundary only at its canonical seams.

## Impact

- Code: `curation.py`, `writer_lease.py`.
- Behaviour: other writers commit between a curation step's seams. A page changed during a step's unlocked preflight makes the leaf refuse with its own stale-binding code, recorded as a failed attempt, where the wide hold made the writer wait instead.
- Errors: a second call on a run in flight gets `CURATION_RUN_BUSY` with `status: retryable` and `committed: false`. A busy vault at a self-guarded leaf's commit reaches the caller as a retryable `MUTATION_BUSY`; the next resume runs the step again.
- Undo: set `EXOMEM_WIDE_MUTATION_BOUNDARY=1`, or revert the change. No stored format changes.
