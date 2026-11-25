## 1. Measure

- [x] 1.1 Add the governed two-flat-folder fixture (10,001 Notes, 10,001 Sources, 10,000 governed referrers) to the scale instrument, and record the build time, page latency and concurrent Records latency on a frozen tree.
  - Done 2026-11-25 on a laptop, frozen tree f8b1288ce, owner-only governed scope over the Notes folder. Under the 50 s request budget every build stopped at 50.0 s and the job never completed (three restarts). Uncapped, the build took 118 s and completed; 10,001 entries over 79 pages, median 83 ms, largest 321 ms, Records median 45 ms during the build. Index and governance generations did not move during the build.

## 2. Governed build

- [x] 2.1 Resolve physical names from one scoped folder listing per build, keyed by folder identity and change stamp, with a test that a new alias spelling still refuses.
- [x] 2.2 Give the background build its own budget or checkpointed progress, chosen from the 1.1 measurement, and prove that cancellation and writer liveness still hold.
  - Chosen: a separate ten-minute build budget. With 2.1 the same governed build takes 28-29 s and is linear in referrers, so checkpointed progress would add machinery for no measured need.
- [x] 2.3 Bind the final snapshot check to the generations that can change the report, with one test that an unrelated generation move keeps the report and one that a relevant change refuses it.
- [x] 2.4 Return a typed public error for an interrupted or cancelled preview job.

## 3. Dependent release

- [x] 3.1 Release a referrer with a container parent or bridge dependency from held evidence plus current proof of each dependency, with zero rereads of the referrer, and fall back when any dependency changes.
  - Container members, extraction parts and scene frames release this way. Bridge rows keep full verification (see design.md).

## 4. Delivery

- [ ] 4.1 Rerun the governed and ungoverned fixtures on the same instrument, then get an independent review and required CI, and confirm on the installed release.
