## 1. Measure

- [x] 1.1 Add the governed two-flat-folder fixture (10,001 Notes, 10,001 Sources, 10,000 governed referrers) to the scale instrument, and record the build time, page latency and concurrent Records latency on a frozen tree.
  - Done 2026-11-25 on a laptop, frozen tree f8b1288ce, owner-only governed scope over the Notes folder. Under the 50 s request budget every build stopped at 50.0 s and the job never completed (three restarts). Uncapped, the build took 118 s and completed; 10,001 entries over 79 pages, median 83 ms, largest 321 ms, Records median 45 ms during the build. Index and governance generations did not move during the build.

## 2. Governed build

- [x] 2.1 Resolve physical names from one scoped folder listing per build, keyed by folder identity and change stamp, with a test that a new alias spelling still refuses.
- [x] 2.2 Give the background build its own budget or checkpointed progress, chosen from the 1.1 measurement, and prove that cancellation and writer liveness still hold.
  - Chosen: a ten-minute job budget that includes report staging and atomic publication. With 2.1 the same governed build takes 28-29 s and is linear in referrers, so checkpointed progress would add machinery for no measured need.
- 2.3 Deferred. Keep the exact snapshot check. See the design for the evidence needed to revisit narrower invalidation.
- [x] 2.4 Return a typed public error for an interrupted or cancelled preview job.

## 3. Dependent release

- [x] 3.1 Release an eligible container member, extraction part or scene frame using held evidence and current proof of its dependencies. Read zero referrer bytes at release and use full verification when a dependency cannot be proved.
  - Parts and frames resolve the same physical parent as ordinary membership. Two parts with one settled parent require one parent byte read per report request. Bridge rows keep full verification.

## 4. Delivery

- [ ] 4.1 Rerun the governed and ungoverned fixtures on the same instrument, then get an independent review and required CI, and confirm on the installed release.
  - Governed runs on Linux with Python 3.11 and 10,000 referrers. Frozen tree `f8b1288ce` built in 170.1 s with a raised budget. Paging stopped on repeated WARMING responses. The bounded-cache candidate built in 31.5 s and served all 79 pages, median 82 ms, max 145 ms. All 29 concurrent Records reads returned a row, median 97 ms, max 462 ms.
  - Ungoverned runs on the same fixture. Main `ccb788663` built in 31.8 s after 280.9 s of setup. The candidate built in 9.6 s after 39.7 s of setup. Both served all 79 pages. The candidate's pages took a median of 40 ms and a max of 89 ms. All ten concurrent Records reads returned a row.
