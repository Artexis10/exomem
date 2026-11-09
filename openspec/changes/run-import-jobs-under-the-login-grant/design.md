## Context

Today, `importer._bound_principal` stores the caller's `origin_session` (`session_id`, `generation`, `audience`) in the job binding. Before each batch, `importer._grant` calls `auth_sessions.origin_session_active_sync`, which runs `authority.validate_binding` on that one access session. `_validate_record_authoritatively` rejects the session once `expires_at`, which is the issue time plus 3600 seconds, has passed.

Each access `SessionRecord` already carries the `family_id` of the refresh family it was issued from. `RefreshFamilyRecord` holds the family's status, issuer, audience, identity and generation. `SessionAuthority._family_is_active` decides whether a family is live, and `_revoke_family` ends a family on logout, revocation or refresh reuse.

`JOB_WINDOW_SECONDS = 3600` is a second clock. `_step` pauses a job with `time_cap` once `window_expires` passes, and only an explicit continuation renews it.

## Goals / Non-Goals

**Goals**

- A healthy import of any length runs to completion without a continuation.
- The job's authority is rechecked before every batch and at commit, and ends as soon as the owner's login ends.
- Every pause reason and every deadline that status shows is true.

**Non-Goals**

- No automatic resume of a job whose authority really ended. That still needs an explicit continuation from the same principal.
- No change to source release, target authority, cancel, status authorization, or host takeover rules.
- No change to how long access or refresh tokens live.

## Decisions

### D1. Bind the refresh family, not the access session

- At start and at resume, the job binding stores `origin_family`: the `family_id` of the caller's access session and the family's generation. The authority that verified the request resolves both, without reading the bearer again.
- `origin_session` stays in the binding for audit and for principals without a family.
- The family id is not a credential. Knowing it grants nothing, because liveness is decided only on the server.

### D2. Recheck family liveness before every batch

- `_grant` asks the authority whether `origin_family` is live, through one read-only method that reuses `_family_is_active`.
- A job whose binding has no family falls back to today's access-session check.
- A family that is revoked or logged out, has the wrong issuer or audience, has a disallowed identity, or is in an old generation makes the batch raise `_Lost`. The job then pauses with `authority_lost`, as today.

### D3. The window stops only a job with no live family

- For a job bound to a family, `_step` does not pause it at `window_expires`.
- The binding's window fields stay in the schema and keep recording when the job was last started or resumed.
- Status replaces `window.expires_at` with `runs_while: "login_active"` for such a job.
- For a job without a family, the window is capped at the bound access session's expiry. When it passes, the job pauses with `time_cap`, and status shows that real deadline.
- Alternative rejected: renewing the window from the job's own ticks. That is ambient authority, which the import requirement forbids.
- Alternative rejected: a separate minted job grant with its own lifetime. It adds a credential to store, rotate and revoke, while the refresh family already has exactly the lifetime and revocation of the owner's consent.

### D4. Wording an agent can repeat

- Status for a running family-bound job says the job runs until done while the login that started it stays active.
- A pause from family loss says the login that started the job ended, and that a continuation from a current login resumes it.
- A `time_cap` pause says the session that started the job expired, and that a continuation resumes it from the same row.
- Error codes and identifiers stay in the diagnostics part.

## Risks / Trade-offs

- **A job keeps running after the client disconnects, for as long as the owner stays logged in.**
  - Accepted. The job is bounded by its source and can be cancelled.
  - Logout and revocation end it at the next batch.
- **Family liveness reads the session store before each batch.**
  - The read is one keyed lookup, the same cost as today's access-session lookup.
  - The authority's existing stale-grace cache applies.
- **Jobs bound before this change carry no family.**
  - They keep today's behaviour until they are resumed. The next continuation binds a family.

## Migration

- No store schema change. The family id lives in the existing `binding_json`.
- Task 1.1 verifies that an older release reading a binding with the extra key still loads the job, and that a job it resumes binds the session, as before.
