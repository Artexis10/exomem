## 1. Seams

- [x] 1.1 Factor `_step_boundary` out of the seal and the compensation seal, and use it for each run-store write.
- [x] 1.2 Add the per-run claim and `CURATION_RUN_BUSY` to `apply`, `resume`, `apply-compensation`, the compensation proposal and an episode leaf's invocation.
- [x] 1.3 Declare `SELF_GUARDED_STEP_KINDS`. Run a scoped step's probes and leaf outside curation's hold, and settle it under one short hold.
- [x] 1.4 Check the plan identity and the bindings, and record approval, under one short hold.
- [x] 1.5 Narrow the dispatcher through `curation.guards_own_boundary`, and keep `EXOMEM_WIDE_MUTATION_BOUNDARY`.

## 2. Proof

- [x] 2.1 A note save commits with no boundary wait while a curation step's leaf preflight is parked. The step's receipt, witness and page bytes equal a run under the wide hold.
- [x] 2.2 A second resume of a run in flight gets `CURATION_RUN_BUSY`, and the step commits once.
- [x] 2.3 A leaf refused by a busy vault reports a retryable, uncommitted `MUTATION_BUSY`, and the next resume commits the step once.
- [x] 2.4 A link rewrite, which takes no guard of its own, commits under the boundary.
- [x] 2.5 The existing crash-seam tests pass unchanged.
- [x] 2.6 A move with a `fields` patch refuses with `SOURCE_CHANGED` when another writer changed the source after the seal, and that writer's change survives.
- [x] 2.7 A save to a step's page between its leaf commit and its settle hold leaves the step committed, and the undo proposal reports it as stale.
- [x] 2.8 A witness that a failed batch's rollback could not remove is not taken as a commit: the run blocks as uncertain and holds no receipt.
- [x] 2.9 Another process refuses a step while its leaf is in flight, and the leaf then commits. The step gets its committed receipt after the refusal, and the run completes.
- [x] 2.10 When that call stops before its settle hold, `status` reports the step as `receipt-required`, and the next resume records it as `recovered-committed`.
- [x] 2.11 Another process refuses a move while its leaf is in flight, and the call stops between its rename and its batch. The next resume adopts the rename and records `recovered-committed`.

## 3. Closure

- [ ] 3.1 After the release is live, a day of the personal service's mutation journal shows curation holds with a p99 of at most 5 s and none over 30 s. A 1 s budget also needs the separate change to the semantic commit seam.
- [ ] 3.2 Archive this change with `openspec archive scope-curation-holds-to-canonical-seams` after task 3.1, and run `openspec validate --all --strict` before and after.

## Follow-ups for separate changes

- The compensation proposal derives the forward run's committed steps from receipts only. A forward step whose leaf committed without a receipt is then left out of the undo until a resume records it. Such a step can be in flight in another process, stopped before its settle hold, or behind another process's refusal. A crash under the wide hold leaves the same state. Under the seal hold, refuse with a retryable code while the forward run's next step holds a witness with no receipt.
- A store-mode move commits through `mutate_in_store`, which never calls the before-rename stage, so it does not recheck the source's sealed hash. Review reports that such a run already ends `CURATION_OUTCOME_UNCERTAIN` before this change. Pass the sealed hash as an explicit source expectation, as `_adopt_rename` does with `source_hash=binding["before_hash"]`.
- Planning and Preservation steps recover a found witness with `live=False`. A witness that a failed batch's rollback could not remove can then be recovered as committed without a live check. This predates this change. Give that recovery a proof that the batch committed, as the live check gives the other kinds.
