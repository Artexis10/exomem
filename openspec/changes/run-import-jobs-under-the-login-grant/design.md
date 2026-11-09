## Context

`importer._bound_principal` stores the caller's verified `client_binding` and `origin_session` (`session_id`, `generation`, `audience`) in the job binding. Before each batch, `importer._grant` calls `auth_sessions.origin_session_active_sync`, which runs `authority.validate_binding`, and from there `_validate_record_authoritatively`. That function checks the session's status, issuer, audience, identity and generation. For an access session (schema 2), it also rejects the session once `expires_at`, the issue time plus 3600 seconds, has passed.

A session with an `expires_at` always carries a `family_id`, and a session without one has neither (`auth_sessions.py`, the access record's structural check). The sessions without an expiry are:

- OAuth sessions issued without `offline_access`
- local-ingress sessions issued by `exomem auth issue-local`

Every per-session revocation also revokes the family: `revoke_bearer` and `tombstone` both do. Refresh reuse calls `_revoke_family`. `_family_is_active` checks the family's status, issuer, audience, identity and generation.

`_grant` also raises `_Lost` once a bound governance authorization session's `expires_at` passes. That session lives at most one hour (`MAX_SESSION_TTL_SECONDS`).

`JOB_WINDOW_SECONDS = 3600` is a separate clock. `_step` pauses a job with `time_cap` once `window_expires` passes, and only an explicit continuation renews it.

Cloud cells carry the gateway's family in the origin binding, but `cloud_origin.matches` is false outside a request. Stdio, CLI and library owners carry no origin session.

## Goals / Non-Goals

**Goals**

- A healthy import of any length runs to completion without a continuation, for every principal whose login the server can revalidate.
- Authority is still rechecked before every batch and at commit, and ends as soon as the login ends.
- Every pause reason and every deadline that status shows is true.

**Non-Goals**

- No automatic resume of a job whose authority really ended. That still needs an explicit continuation from the same principal.
- No change to source release, target authority, cancel or status authorization, host takeover, or the lifetime of any token or session.
- No change for Cloud cells, or for stdio, CLI and library owners.

## Decisions

### D1. Revalidate the bound origin, minus one access token's lifetime

- Add one background revalidation to the session authority. It loads the bound session by `session_id` and applies every check in `validate_binding`:
  - status
  - the issuer and audience of the authority
  - client id equal to the bound client binding
  - identity allowed
  - current generation
  - structural validity
- An access session's own `expires_at` is the one check it skips. In its place, the session's family must pass `_family_is_active`, and the family's client id must equal the bound client.
- A session without an expiry has no family, so no family check applies.
- The job binding stays unchanged, and no new key is stored. A job bound before this change gets the fix at its next batch, without a continuation.
- Alternatives rejected:
  - Storing the family id in the binding. It duplicates what the session record already carries, and it needs a migration case.
  - A separately minted job grant. It is a new credential to store, rotate and revoke, while the login already has exactly the lifetime and revocation of the owner's consent.
  - Capping the window at the token's expiry and relabelling the pause. Status becomes honest, but the job still cannot complete unattended.

### D2. Three outcomes, so a store failure is not a revocation

The revalidation returns one of three results:

- **valid:** the batch proceeds.
- **not valid:** the batch raises `_Lost`, and the job pauses with `authority_lost`, as today.
- **unreadable:** the session store could not be read. The job takes the existing host-local `store_unavailable` pause and a later tick retries.

Today `SessionStoreUnavailable` reads as lost authority. Once the hourly expiry is gone, that would become the main false `authority_lost`.

### D3. Which clock still applies

| The job's origin | Deadline | Pause at the deadline |
|---|---|---|
| Origin session that stays valid (expiring access session with a live family, or a session without an expiry) | none | none |
| Also bound to a governance authorization session | that session's `expires_at`, at most one hour | `time_cap` |
| No origin session (stdio, CLI, library, Cloud cell) | the one-hour window, as today | `time_cap` |

- These are the only deadlines.
- The login never extends a governance authorization session.
- Status for a job without a deadline reports `runs_while: "login_active"` and no `expires_at`.
- Status for a job with a deadline shows that deadline.
- The window fields stay in the store and keep recording when the job last started or resumed.

### D4. Wording an agent can repeat

- A running job without a deadline: "This import runs until it finishes, while the login that started it stays valid."
- A pause from lost authority: "The login or access that started this import ended. A continuation from a current login resumes it from the same row."
- A `time_cap` pause after a governance session: "The authorization session that started this import expired. A continuation resumes it from the same row."
- A `time_cap` pause after the window: "This import's one-hour run window ended. A continuation resumes it from the same row."
- Error codes go in diagnostics. Session ids, family ids and client ids appear nowhere in status, receipts or diagnostics.

## Risks / Trade-offs

- **A job outlives its client's disconnect, for as long as the login stays valid.**
  - A refresh family has no absolute or idle lifetime today, so in practice "valid" means "until it is revoked".
  - Accepted. The job only completes one fixed, owner-started import into one collection.
  - Logout and revocation end it at the next batch.
- **A leaked one-hour access token could start an import that then runs to completion, unless the owner revokes the login.**
  - Today, such a token's job dies within the hour.
  - The risk is low. The holder of a leaked token can already import within that hour. What it gains is completion of one bounded job, not ambient authority: every batch rereads the session store, so this is not an initial cached grant.
- **The revalidation reads the session store before each batch.**
  - That is one keyed lookup plus one family lookup, close to today's cost.
  - No cache is involved, because the bearer-keyed validation cache does not serve the background path.

## Migration

None. Bindings are unchanged, and the new rule applies at each job's next batch.
