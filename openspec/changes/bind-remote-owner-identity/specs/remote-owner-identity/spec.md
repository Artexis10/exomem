## ADDED Requirements

### Requirement: The host binds one remote identity as the owner explicitly

The system SHALL treat a remote OAuth principal as the owner only when the host's service
configuration names that principal's provider subject in `EXOMEM_OWNER_OAUTH_SUBJECT`,
written `github:` followed by a positive ASCII-decimal GitHub user id without leading
zeros. The binding SHALL be read only from the process environment supplied by the host's
service configuration. No vault file, policy document, request, header, tool argument,
token claim, or command SHALL set, widen, or override it. An absent binding SHALL leave
every remote principal resolving exactly as before. A malformed binding SHALL be treated
as absent, SHALL be reported by doctor as a failure and by one content-free startup log
line, and SHALL NOT prevent the service from starting.

#### Scenario: An unconfigured install keeps the separate remote principal

- **WHEN** `EXOMEM_OWNER_OAUTH_SUBJECT` is unset and the allowed GitHub account calls the
  remote connector
- **THEN** the call resolves to its `principal:` audience, not the owner

#### Scenario: The configured subject acts as the owner

- **WHEN** the binding names the allowed GitHub account's id and that account calls the
  remote connector
- **THEN** the call resolves to the owner audience

#### Scenario: A malformed binding changes nothing

- **WHEN** the binding is a login, an email address, lacks the provider prefix, has a
  leading zero, uses non-ASCII digits, or carries trailing text
- **THEN** the service starts, remote calls resolve as if the binding were unset, and
  doctor reports the binding as malformed

#### Scenario: Content cannot create the binding

- **WHEN** a vault page, policy document, tool argument or request header contains the
  binding's name and a matching value
- **THEN** no request resolves to the owner because of it

### Requirement: Only this install's verified session matches the binding

A request SHALL match the binding only when its access token was issued and validated by
this install's durable session authority, its issuer equals this install's configured
base URL, and its typed GitHub user id equals the bound id. Credentials that reached the
server another way SHALL NOT match: raw bearer headers, other verifiers, hosted and cell
service credentials, and claims copied into another token. The match SHALL be evaluated
on every request.

#### Scenario: A forged bearer on loopback HTTP is not the owner

- **WHEN** the loopback server runs without OAuth and a caller presents a bearer equal to
  the bound id or its prefixed form
- **THEN** the call does not resolve to the owner

#### Scenario: A token from another install is not the owner

- **WHEN** a token issued by a different install is presented
- **THEN** it is rejected before principal resolution and never resolves to the owner

#### Scenario: Rebinding takes effect on the next request

- **WHEN** the host removes or changes the binding and restarts
- **THEN** the next request from an existing session resolves to its `principal:`
  audience without the session being revoked

### Requirement: An owner-equivalent remote session is the owner, labelled remote

A request that matches the binding SHALL resolve to the owner audience for every consumer
of the audience, including release decisions, scope caps, owner-only governance
operations, inspection, and per-audience state. It SHALL keep its remote OAuth issuer
family, so authorization sessions, session grants and vocabulary authorities never move
between the owner's local and remote doors. Its retry and idempotency scope and its
call-ledger caller hash SHALL remain those of the remote identity. Call and activation
ledgers SHALL record `principal_kind: owner-oauth` for it.

#### Scenario: A default-deny scope admits the owner's remote session

- **WHEN** an item belongs only to a `default_deny` scope with no rule naming the caller,
  and the owner's bound remote session reads it
- **THEN** the item is released as it is to the owner locally

#### Scenario: Owner-only governance works remotely and stays traceable

- **WHEN** the bound remote session commits a governance proposal
- **THEN** the commit succeeds, and the call ledger row for its request id records
  `principal_kind: owner-oauth` with the remote caller hash

#### Scenario: A local governance session does not resume remotely

- **WHEN** the owner opens an authorization session locally and the bound remote session
  presents that capability
- **THEN** the credential is refused because the issuer family differs

#### Scenario: Local and remote owner doors share per-audience state

- **WHEN** the owner's local door and bound remote session each record under one episode
  key or set a prominence level
- **THEN** both act on one owner ledger and one preference

#### Scenario: Rules naming the former remote audience stop applying

- **WHEN** a rule or grant names the remote identity's `principal:` audience and the
  binding becomes active
- **THEN** that rule or grant no longer applies to the bound session, and doctor reports it

### Requirement: No migration of former remote-audience state

The system SHALL NOT rewrite, merge or read through per-audience state recorded under the
former remote audience when a binding becomes active. That state SHALL remain intact and
SHALL apply again if the binding is removed.

#### Scenario: Removing the binding restores the former state

- **WHEN** a remote preference was set before the binding, the binding is enabled, and it
  is then removed
- **THEN** the remote session sees its original preference again

### Requirement: The binding is visible to the host owner

Doctor SHALL report the binding as unset, active, mismatched with the allowed sign-in
account, or malformed, without printing the id. When the binding is active, doctor SHALL
report the number of policy rules and grants that name the former remote audience. The
session listing SHALL mark each session whose identity matches the binding.

#### Scenario: Doctor explains an unreachable binding

- **WHEN** the binding names an id other than the allowed sign-in account
- **THEN** doctor fails the check and says owner equivalence can never apply

### Requirement: Hosted cells never use a personal binding

A hosted cell SHALL clear the binding from its environment. No hosted gateway principal or
cell service credential SHALL match it. A cloud cell MAY bind only its own cell identity,
under a requirement of its own change.

#### Scenario: A hosted cell ignores a planted binding

- **WHEN** a hosted cell starts with the binding in its environment
- **THEN** the binding is cleared, and every principal resolves as before

### Requirement: Sessions of an account no longer allowed to sign in stop validating

Session validation SHALL reject a durable session or refresh family whose GitHub user id
is not the account currently allowed to sign in, so changing the allowed account ends
every session of the former one without a separate revocation step. An install that has
no configured allowed account SHALL keep validating as before.

#### Scenario: Changing the allowed account ends the former account's sessions

- **WHEN** a session was issued to one GitHub account and the host changes the allowed
  sign-in account to another and restarts
- **THEN** the former account's session and its refresh family no longer validate
