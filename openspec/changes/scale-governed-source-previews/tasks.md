## 1. Measure

- [ ] 1.1 Add the governed two-flat-folder fixture (10,001 Notes, 10,001 Sources, 10,000 governed referrers) to the scale instrument, and record the build time, page latency and concurrent Records latency on a frozen tree.

## 2. Governed build

- [ ] 2.1 Resolve physical names from one scoped folder listing per build, keyed by folder identity and change stamp, with a test that a new alias spelling still refuses.
- [ ] 2.2 Give the background build its own budget or checkpointed progress, chosen from the 1.1 measurement, and prove that cancellation and writer liveness still hold.
- [ ] 2.3 Bind the final snapshot check to the generations that can change the report, with one test that an unrelated generation move keeps the report and one that a relevant change refuses it.
- [ ] 2.4 Return a typed public error for an interrupted or cancelled preview job.

## 3. Dependent release

- [ ] 3.1 Release a referrer with a container parent or bridge dependency from held evidence plus current proof of each dependency, with zero rereads of the referrer, and fall back when any dependency changes.

## 4. Delivery

- [ ] 4.1 Rerun the governed and ungoverned fixtures on the same instrument, then get an independent review and required CI, and confirm on the installed release.
