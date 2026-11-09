## 0. Owner decision

- [ ] 0.1 The owner approves binding import jobs to the login grant, so that a healthy import runs to completion without hourly continuations.

## 1. Bind and recheck the login grant

- [ ] 1.1 Store `origin_family` (family id and generation) in the job binding at start and resume, resolved by the authority that verified the request. Prove that an older release still loads a job whose binding has the extra key.
- [ ] 1.2 Add one read-only family liveness check to the session authority, reusing `_family_is_active`. Use it in `importer._grant` for family-bound jobs, and keep the access-session check for jobs without a family.
- [ ] 1.3 Stop pausing family-bound jobs at the window. For session-bound jobs, cap the window at the session's expiry so that they pause with `time_cap`.
- [ ] 1.4 Status and pause wording follow design D4. Codes and identifiers stay in diagnostics.

## 2. Proof

- [ ] 2.1 Tests, with the token clock advanced and not slept:
  - A job outlives its first access token and completes.
  - Logout pauses it with `authority_lost`, and family revocation does the same.
  - A generation change pauses it.
  - A continuation from the same principal resumes it, and one from another principal refuses.
  - A job with no family pauses with `time_cap` at its session expiry, and status showed that expiry before the pause.
- [ ] 2.2 On the live service after release, run an import longer than one hour. It completes with no continuation, and the service log shows no `session_rejected reason=expired` pause for it.

## 3. Closure

- [ ] 3.1 After merge, synchronize this change's delta and archive it with `openspec archive`, validating before and after.
