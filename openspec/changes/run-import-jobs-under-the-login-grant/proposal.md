## Why

A long import cannot finish unattended. An import job binds the origin session of the MCP call that started or resumed it. Before each batch, the job rechecks that session through `validate_binding`, which also rejects an access session once its one-hour lifetime (`ACCESS_TOKEN_TTL_SECONDS`) has passed. The client obtains its token before the call, so the token always expires at or before the job's own one-hour window. Once it expires, the job pauses as `partial` with reason `authority_lost`.

In one import of about ten million rows, every pause matched an `event=session_rejected reason=expired` line in the service log. Three effects follow:

- A job of several hours needs a person or an agent to send a continuation every hour.
- The reason `authority_lost` reads as a permission problem, but nothing was revoked.
- Status shows `window.expires_at` as later than the real stop. A job started with a token issued 40 minutes earlier stops 20 minutes after it starts, while its status still promises an hour.

An access token is short-lived on purpose, so that a leaked token stops working soon. The owner's consent to the job is the login itself. An expiring access session always belongs to a refresh-token family, which the server keeps, and the server already ends that family on logout, revocation, refresh reuse, a generation change or a disallowed identity. Background work that runs while the login stays valid, not while one short access token lives, is the usual practice for delegated offline access.

## What Changes

- Before each batch and at commit, the job revalidates its bound origin session as today: its status, issuer and audience, client, identity and generation. The only difference is that an access session's own expiry no longer ends the job. An expiring access session must instead have a live refresh family.
- A session that has no expiry has no family to check, and is revalidated as today. Local-ingress sessions and OAuth sessions issued without `offline_access` are of this kind.
- A job whose origin is still valid does not pause at the fixed one-hour window. It runs until its source is exhausted, the owner cancels it, or its authority ends.
- Authority ends when the session or its family is revoked or logged out, the generation changes, the identity is disallowed, the source release is revoked, or the target authority is lost. Each of these pauses the job with `authority_lost`, as today.
- A job started with a governance authorization session keeps that session's expiry, at most one hour, as its deadline. Status shows the deadline, and the job pauses with `time_cap` when it passes. The login never extends it.
- A job whose origin the service cannot revalidate without a live request keeps the fixed one-hour window and pauses with `time_cap` when the window ends. This covers stdio, CLI and library owners, which carry no origin session, and Cloud cells, which bind an origin session that only the gateway can vouch for during a request. Its status keeps today's deadline.
- If the session store cannot be read, the job takes the existing host-local `store_unavailable` pause and a later tick retries, on a bounded backoff. Only a definite "not valid" answer pauses it with `authority_lost`.
- Continuation, cancel and status keep their current authorization, and a different principal still cannot inherit a job.

## Capabilities

### New Capabilities

- `import-job-login-grant`: an import job's authority is the still-valid login that started it, rechecked before every batch and at commit. A short access token's expiry does not end it, and status shows only deadlines the job keeps.

### Modified Capabilities

- `collection-query-engine`: no canonical spec exists yet, so this change edits the active change `add-collection-query-engine` directly. Three places name the one-hour job cap, and each now defers to `import-job-login-grant`:
  - the requirement "Import jobs retain continuing source and target authority"
  - the design's time-cap line
  - task 7.4

The canonical `release-gate` requirement for durable import jobs ("Each authority check SHALL revalidate the live origin") stays satisfied word for word. Each check still revalidates the live origin. Only the access lifetime of an origin with a live family no longer counts as loss.

## Impact

`src/exomem/collection_store/importer.py`:

- `_grant` revalidates the bound origin with the access-lifetime exception.
- `_step` applies the window only to jobs whose origin the service cannot revalidate without a live request.
- The `lapsed` check in `_continue` changes the same way.
- The `time_cap` branch of `_status` and the status window fields change accordingly.
- `contract()` `limits.window_seconds` and preview `limits.job_seconds` are replaced by a `run_window` statement and a `job_window` limit that say which clock applies.
- The module docstring is updated.

`src/exomem/auth_sessions.py`:

- One background revalidation that applies every `validate_binding` check except an access session's own expiry, and then requires its refresh family to be active.
- It returns a three-way result: valid, not valid, or store unreadable.

Tests:

- A job outlives its first access token and completes.
- Logout, family revocation, refresh reuse, a generation change and a disallowed identity each pause the next batch with `authority_lost`.
- A client mismatch refuses.
- A session without an expiry runs through.
- A job with a governance authorization session pauses with `time_cap` at that session's expiry, and status showed it.
- A stdio owner's job and a Cloud cell's job keep the one-hour window.
- An unreadable session store pauses with `store_unavailable`, a later tick resumes, and the retry backoff is bounded.
- Another principal's continuation still refuses.
