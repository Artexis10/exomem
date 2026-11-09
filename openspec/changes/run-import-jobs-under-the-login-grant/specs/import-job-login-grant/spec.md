## ADDED Requirements

### Requirement: An import job runs under the login grant that started it

An import job SHALL bind the login grant (the refresh-token family) of the call that starts or resumes it, together with its principal and access session. Before each batch and at commit, the job SHALL recheck that the grant is live. A live grant is one that is active, issued by this server for this audience, allowed for this identity and in the current generation. The expiry of the access token used to start the job SHALL NOT pause it. A job whose grant is live SHALL NOT pause at a fixed window. A job SHALL pause with `authority_lost` when its grant is revoked, logged out or superseded by a generation change, or when its source release or target authority ends. Only an explicit continuation from the same principal SHALL resume it.

#### Scenario: A long import outlives its first access token

- **WHEN** an owner starts an import whose batches take longer than one access-token lifetime, and the owner's login stays active
- **THEN** the job runs to `complete` with no continuation, and every batch rechecks the login grant before it commits

#### Scenario: Logging out stops the job at the next batch

- **WHEN** the owner logs out, or the refresh family is revoked, while a job runs
- **THEN** the next batch refuses without advancing the checkpoint, the job pauses with `authority_lost`, and a continuation from a current login of the same principal resumes it from the same row

#### Scenario: A generation change stops the job

- **WHEN** the session authority's generation changes while a job runs
- **THEN** the next batch refuses and the job pauses with `authority_lost`

#### Scenario: Another principal cannot continue the job

- **WHEN** a different principal sends a continuation for a paused job
- **THEN** the continuation refuses, exactly as for a job bound to an access session

### Requirement: A job without a login grant reports its real deadline

A job whose principal has no refresh family SHALL keep its access-session binding. The job's deadline SHALL be the earlier of its window and the bound session's expiry. When that deadline passes, the job SHALL pause with `time_cap`, and status SHALL show that deadline. Status for a grant-bound job SHALL state that the job runs while the login that started it stays active, and SHALL NOT show a deadline the job will not keep.

#### Scenario: A session-bound job pauses at its session expiry

- **WHEN** a job bound only to an access session reaches that session's expiry before its window ends
- **THEN** the job pauses with `time_cap` at that moment, and the status read just before showed the session expiry as its deadline
