## Why

A long import cannot finish unattended. An import job binds the access session of the MCP call that started or resumed it. An access token lives one hour (`ACCESS_TOKEN_TTL_SECONDS`), and the client obtains it before the call, so it always expires at or before the job's own one-hour window. Each batch rechecks the bound access session, and once that token expires the job pauses as `partial` with reason `authority_lost`.

In one import of about ten million rows, every pause matched an `event=session_rejected reason=expired` line in the service log. Three effects follow:

- A job of several hours needs a person or an agent to send a continuation every hour.
- The reason `authority_lost` reads as a permission problem, but nothing was revoked.
- Status shows `window.expires_at` as later than the real stop. A job started with a token issued 40 minutes earlier stops 20 minutes after it starts, while its status still promises an hour.

The access token is the wrong thing to bind. It is short-lived on purpose, so that a leaked token stops working soon. The owner's consent to the job is the login itself. Exomem is its own OAuth server, and every access token belongs to a refresh-token family. The server already checks whether a family is live: active, issued by this server for this audience, allowed for this identity, and in the current generation. Logout, revocation and refresh-token reuse each end the family at once. Background work that runs under the login, not under one short access token, is the usual practice for delegated offline access.

## What Changes

- An import job binds the login grant, meaning the refresh-token family, of the call that starts or resumes it, together with the principal and the access session it already binds.
- Before each batch and at commit, the job checks that the family is live. The access token's expiry alone no longer pauses the job.
- The job runs until its source is exhausted, the owner cancels it, or its authority ends. Authority ends when the family is revoked or logged out, the generation changes, the source release is revoked or the target authority is lost. Each of these pauses the job with `authority_lost`, as today.
- The fixed one-hour job window no longer stops a job whose login grant is live. Status reports what the job runs under ("runs while the login that started it stays active") and no invented deadline. An agent therefore never has to resume a healthy job.
- A principal whose login has no refresh family keeps today's session binding. When its bound session expires, the job pauses with reason `time_cap`, which names the expiry and not a revocation.
- Continuation, cancel and status keep their current authorization, and a different principal still cannot inherit a job.

## Capabilities

### New Capabilities

- `import-job-login-grant`: an import job's authority is the live login grant that started it, rechecked before every batch, and ends only when that grant, the source release or the target authority ends.

### Modified Capabilities

- `collection-query-engine`: no canonical spec exists yet, so this change edits the active change `add-collection-query-engine` directly. In the requirement "Import jobs retain continuing source and target authority", the bound "live authorization session/grant" is the login grant defined by `import-job-login-grant`. An access token's expiry alone does not pause a job.

## Impact

- `src/exomem/collection_store/importer.py`: the binding gains the family id. The `_grant` check uses family liveness. The window no longer pauses a job with a live family. Status gains the honest window wording.
- `src/exomem/auth_sessions.py`: one read-only liveness check by family id, which reuses `_family_is_active`.
- Tests: a job outlives its first access token and completes; logout or family revocation pauses the next batch with `authority_lost`; a generation change pauses it; a principal with no family pauses with `time_cap` when its session ends; another principal's continuation still refuses.
- Security: authority is still rechecked before every batch, under the same server-side revocation as the login itself. No new credential is minted or stored, and only the family id, which is not a secret, is bound.
