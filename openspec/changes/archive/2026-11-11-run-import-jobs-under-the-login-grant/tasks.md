## 0. Owner decision

- [x] 0.1 The owner approves this change (approved 2026-11-09): a healthy import runs to completion while the login that started it stays valid, rechecked before every batch, with no hourly continuation.

## 1. Revalidate the login, not one token's lifetime

- [x] 1.1 Add the background origin revalidation to the session authority (design D1, D2). It applies every `validate_binding` check except an access session's own expiry, requires that session's family to be active for the same client, and returns valid, not valid or unreadable.
- [x] 1.2 Use it in `importer._grant` and in the `lapsed` check of `_continue`. A store-unreadable answer takes the host-local `store_unavailable` pause, retried on a bounded backoff.
- [x] 1.3 Apply deadlines as design D3 says: none for a still-valid origin; the governance authorization session's expiry where one is bound; the one-hour window only for a job whose origin the service cannot revalidate without a live request (a Cloud cell, or a stdio, CLI or library owner with no origin session).
- [x] 1.4 Update status, the `time_cap` branch of `_status`, `contract()` `limits.window_seconds`, preview `limits.job_seconds` and the module docstring to the D3 and D4 wording, with no session, family or client identifiers.

## 2. Proof

- [x] 2.1 Tests, with the token clock advanced and not slept:
  - A job with an expiring access session outlives that token and completes.
  - Logout, family revocation, refresh reuse, a generation change, a disallowed identity and a client mismatch each pause the next batch with `authority_lost`.
  - A session without an expiry runs past one hour, and revoking it pauses the job.
  - A job with a governance authorization session pauses with `time_cap` at that session's expiry, and status showed it.
  - A stdio owner's job and a Cloud cell's job keep the one-hour window.
  - An unreadable session store pauses the job as `store_unavailable`, a later tick resumes it, and the retry backoff is bounded.
  - A continuation from another principal refuses.
- [x] 2.2 On the live service after release, run an import longer than one hour from an MCP client. It completes with no continuation, and the service log shows no expired-session pause for it. **Evidence:** on release 0.171.0, job `dbae47465bee4e4aa5df504f9d8e413d` ran 2026-11-11T09:45:54Z→11:06:41Z (80.8 min) through an MCP OAuth session, past its stamped window boundary at 10:45:54Z with `state=running` and `reason=None`, then `complete`: 816/816 members, 829,100 rows, 8,874 batches, no continuation issued, and `exomem.log` shows no pause, expired-session or authority events for it.
- [x] 2.3 Fold the archive nits: an `authority_lost` batch whose loss lands as a fixed window also closes keeps `authority_lost` (only an expired authorization session reads `time_cap`). **Evidence:** `test_a_batch_that_loses_authority_as_its_window_closes_stays_authority_lost`, red on the old code and green after.

## 3. Closure

- [x] 3.1 After merge, synchronize this change's delta and archive it with `openspec archive`, validating before and after. **Evidence:** implementation merged as `e4102bd21` (#2680), released in v0.171.0; archived by this closure with strict validation passing before and after.
