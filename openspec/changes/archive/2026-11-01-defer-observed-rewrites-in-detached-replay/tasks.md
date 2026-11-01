## 1. Defer instead of retarget

- [x] 1.1 Replace the retarget loop in `_rebase_detached_catalog` with one proof round. Off the barrier: one observation wait, then defer the observed pages and record the original target. Under the barrier: one registry read, no wait. No deferral when a delta needs a source proof. Shared pages defer only when every covering scope names them. Evidence: the rewritten replay tests show that a pass publishes at the original target, that the page stays in the delta from that target, and that a later catch-up serves its newest content.
- [x] 1.2 Send a barrier refusal on a page with an open publication intent to the off-barrier catch-up. Use the path-specific intent check. Evidence: a test where a write is publishing at the barrier.
- [x] 1.3 Telemetry: remove `retargets_exhausted`, rename `retargeted` to `awaited`, and add `deferred`. Evidence: the telemetry test.

## 2. Catch-up advances under sustained writes

- [x] 2.1 Record, per lagging scope, the newest checkpoint of an outpaced catch-up round whose delta was applied after it and for whose absent pages the scope holds no row, and queue a lag-only catch-up for newer events. A round in which a lagging scope stays frozen declines. Evidence: tests where events keep arriving through every step of a round, where one scope stays frozen, where a deleted page comes back before its step, and where a page created after the checkpoint is deleted.

## 3. Proof under sustained writes

- [x] 3.1 Run a local repro on main with a vault of about 10,000 notes, back-to-back import calls and an index page that every call rewrites, with a forced full repair. Evidence: the repro on main reached about 5 new notes per call, against 60 to 150 in the Cloud rehearsal, and every full pass published (0 declined in 4 runs). A local run cannot reach the write rate that exhausts the retargets, so the proof under sustained writes is the Cloud rehearsal in 3.2. The convergence after writes stop is covered by the replay tests of 1.1, where a later catch-up serves the page's newest content.
- [x] 3.2 Read a Cloud large-import rehearsal's `lexical_repair_finished` events and the peak of `catalogue_unchanged_seconds`. Evidence: run 41328793829 (69,300 files staged; one run) against main's run 41294047062:
  - full passes: 12 of 12 published, against 1 of 15 with 14 declined `retargets_exhausted`;
  - `deferred` summed to 24 and `awaited` to 26 across 37 events; 4 catch-ups ended `advanced`, and 12 catch-ups declined, which left the lag to the next full pass;
  - peak `catalogue_unchanged_seconds` 620.5 s, against 4,928 s; a full pass took 530 to 570 s at that size;
  - readiness samples: 499 ready, 1 not ready before the first proof, 27 unread.
  The run ended on the import paging deadline, as main's run did; that limit is apply throughput, not this change.
