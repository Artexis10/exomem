## 0. Owner decision

- [ ] 0.1 The owner approves this change: a healthy import runs to completion while the login that started it stays valid, rechecked before every batch, with no hourly continuation.

## 1. Revalidate the login, not one token's lifetime

- [ ] 1.1 Add the background origin revalidation to the session authority (design D1, D2). It applies every `validate_binding` check except an access session's own expiry, requires that session's family to be active for the same client, and returns valid, not valid or unreadable.
- [ ] 1.2 Use it in `importer._grant` and in the `lapsed` check of `_continue`. A store-unreadable answer takes the host-local `store_unavailable` pause.
- [ ] 1.3 Apply deadlines as design D3 says: none for a still-valid origin; the governance authorization session's expiry where one is bound; the one-hour window only for a job without an origin session.
- [ ] 1.4 Update status, the `time_cap` branch of `_status`, `contract()` `limits.window_seconds`, preview `limits.job_seconds` and the module docstring to the D3 and D4 wording, with no session, family or client identifiers.

## 2. Proof

- [ ] 2.1 Tests, with the token clock advanced and not slept:
  - A job with an expiring access session outlives that token and completes.
  - Logout, family revocation, refresh reuse, a generation change, a disallowed identity and a client mismatch each pause the next batch with `authority_lost`.
  - A session without an expiry runs past one hour, and revoking it pauses the job.
  - A job with a governance authorization session pauses with `time_cap` at that session's expiry, and status showed it.
  - A stdio owner's job keeps the one-hour window.
  - An unreadable session store pauses the job as `store_unavailable`, and a later tick resumes it.
  - A continuation from another principal refuses.
- [ ] 2.2 On the live service after release, run an import longer than one hour from an MCP client. It completes with no continuation, and the service log shows no expired-session pause for it.

## 3. Closure

- [ ] 3.1 After merge, synchronize this change's delta and archive it with `openspec archive`, validating before and after.
