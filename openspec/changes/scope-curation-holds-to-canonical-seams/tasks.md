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

## 3. Closure

- [ ] 3.1 After the release is live, a day of the personal service's mutation journal shows curation holds with a p99 of at most 5 s and none over 30 s. A 1 s budget also needs the separate change to the semantic commit seam.
- [ ] 3.2 Archive this change with `openspec archive scope-curation-holds-to-canonical-seams` after task 3.1, and run `openspec validate --all --strict` before and after.
