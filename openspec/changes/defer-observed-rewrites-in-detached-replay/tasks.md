## 1. Defer instead of retarget

- [x] 1.1 Replace the retarget loop in `_rebase_detached_catalog` with one proof round. Off the barrier: one observation wait, then defer the observed pages and record the original target. Under the barrier: one registry read, no wait. No deferral when a delta needs a source proof. Shared pages defer only when every covering scope names them. Evidence: the rewritten replay tests show that a pass publishes at the original target, that the page stays in the delta from that target, and that a later catch-up serves its newest content.
- [x] 1.2 Send a barrier refusal on a page with an open publication intent to the off-barrier catch-up. Use the path-specific intent check. Evidence: a test where a write is publishing at the barrier.
- [x] 1.3 Telemetry: remove `retargets_exhausted`, rename `retargeted` to `awaited`, and add `deferred`. Evidence: the telemetry test.

## 2. Catch-up advances under sustained writes

- [x] 2.1 Record, per lagging scope, the newest checkpoint of an outpaced catch-up round whose delta was applied after it and whose deleted pages hold no row, and queue a lag-only catch-up for newer events. A round that moves no stored checkpoint declines. Evidence: tests where events keep arriving through every step of a round, where the lag sits in one scope, and where a deleted page comes back before its step.

## 3. Proof under sustained writes

- [ ] 3.1 Run a local repro on main and on the branch. Use a vault of about 10,000 notes and back-to-back import calls of about 100 new notes for 20 minutes, with an index page that every call rewrites. Force a full repair at 60 s. Evidence: at least 3 full passes publish with `deferred` > 0, and the peak of `catalogue_unchanged_seconds` stays near one pass. After the writes stop, the strict proof is current and a search returns the index page's last token.
- [ ] 3.2 Read the next Cloud rehearsal's `lexical_repair_finished` events and the peak of `catalogue_unchanged_seconds`.
