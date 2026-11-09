## ADDED Requirements

### Requirement: An import job runs while the login that started it stays valid

Before each batch and at commit, an import job SHALL revalidate its bound origin session, applying every check that request validation applies:
- status
- issuer and audience
- the bound client
- identity
- generation
- structural validity

An access session's own expiry SHALL NOT end the job. In its place, an expiring access session SHALL require its refresh-token family to be active for the same client. A session without an expiry SHALL be revalidated without a family check. A job whose origin stays valid SHALL NOT pause at a fixed window.

A job SHALL pause with `authority_lost` when the revalidation answers that the origin is not valid, or when its source release or target authority ends. Logout, revocation, refresh-token reuse, a generation change, a disallowed identity and a client mismatch each make the origin not valid. If the session store cannot be read, the job SHALL take the host-local `store_unavailable` pause and retry on a later tick. Only an explicit continuation from the same principal SHALL resume a job paused with `authority_lost`.

#### Scenario: A long import outlives its first access token

- **WHEN** an owner starts an import with an expiring access session whose batches take longer than that token's lifetime, and the owner's login stays valid
- **THEN** the job runs to `complete` with no continuation, and every batch revalidated the origin before it committed

#### Scenario: Ending the login stops the job at the next batch

- **WHEN** the owner logs out, the refresh family is revoked, the refresh token is reused, the generation changes or the identity is disallowed while a job runs
- **THEN** the next batch refuses without advancing the checkpoint, the job pauses with `authority_lost`, and a continuation from a current login of the same principal resumes it from the same row

#### Scenario: A session without an expiry runs through

- **WHEN** a local-ingress owner, or an OAuth session issued without offline access, starts an import that runs longer than one hour
- **THEN** the job runs to `complete` while that session stays active, and revoking the session pauses it with `authority_lost`

#### Scenario: An unreadable session store is not a revocation

- **WHEN** the session store cannot be read during a batch's revalidation
- **THEN** the job pauses as `store_unavailable` without advancing the checkpoint, and a later tick continues it once the store answers

#### Scenario: Another principal cannot continue the job

- **WHEN** a different principal sends a continuation for a paused job
- **THEN** the continuation refuses, exactly as before this change

### Requirement: Status shows only the deadlines a job keeps

A job bound to a governance authorization session SHALL keep that session's expiry as its deadline, and the login SHALL NOT extend it. A job without an origin session SHALL keep the fixed one-hour window as its deadline. When a deadline passes, the job SHALL pause with `time_cap`, and its status SHALL have shown that deadline. Status for a job without a deadline SHALL state that it runs while the login that started it stays valid, and SHALL NOT show a deadline. Status, receipts and diagnostics SHALL NOT show session, family or client identifiers.

#### Scenario: A running job without a deadline says so

- **WHEN** an owner reads the status of a running job whose origin session stays valid
- **THEN** status says the import runs until it finishes while the login that started it stays valid, and shows no `expires_at`

#### Scenario: A governance session's expiry still ends the job

- **WHEN** a job bound to a governance authorization session reaches that session's expiry, and the owner's login is still valid
- **THEN** the job pauses with `time_cap` at that moment, and the status read just before showed that expiry as its deadline

#### Scenario: A job without an origin session keeps the window

- **WHEN** a stdio owner's import runs for one hour
- **THEN** it pauses with `time_cap`, its status showed the window's end, and a continuation resumes it from the same row
