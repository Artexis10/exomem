# Spec Delta

## Purpose

Let service callers reconcile text Source captures from canonical storage after bounded invocation replay expires or a process restarts.

## ADDED Requirements

### Requirement: Caller identity binds an immutable text capture

`capture_source` SHALL accept optional `source_identity` with exactly three nonempty opaque strings: `system`, `record_id`, and `revision`.
Identity SHALL be vault-local and independent of invocation keys. Files and adoption with this identity MUST be refused before retrieval or writes.
Omitting identity SHALL retain existing capture behavior.

#### Scenario: Independent records contain identical text
- **WHEN** two captures have different identities and identical authored inputs
- **THEN** each capture creates its own Source and stable reference

#### Scenario: Revisions arrive out of order
- **WHEN** a caller captures two revisions of one record in any order
- **THEN** both remain separate immutable Sources
- **AND** neither capture infers chronology, predecessor links or retirement

#### Scenario: Identity accompanies an attachment
- **WHEN** a caller supplies source identity with files or adoption
- **THEN** capture refuses with `SOURCE_IDENTITY_TEXT_ONLY` before fetching or writing

### Requirement: Durable reconciliation compares original authored inputs

The initial canonical Source write MUST store typed identity and a digest of normalized original text, title, kind, URL, tags, reason, domain, projects, requested slug and raw protection.
Retry IDs, response detail, compilation hints, timestamps, allocated paths and server IDs MUST NOT affect that digest.
Later metadata updates MUST preserve the original digest.

#### Scenario: Capture response is lost beyond replay retention
- **WHEN** an identical capture arrives after restart or replay expiry with a fresh invocation key
- **THEN** the same stable Source reference returns with explicit `reused` outcome
- **AND** no second Source or capture activity is written

#### Scenario: Metadata changed after capture
- **WHEN** a Source gains compilation back-references before the caller repeats its original capture
- **THEN** reconciliation uses the original capture digest and returns that Source

#### Scenario: Authored inputs conflict
- **WHEN** the same identity arrives with different normalized authored inputs
- **THEN** capture refuses with `SOURCE_IDENTITY_CONFLICT` without writes

### Requirement: Canonical Sources decide identity uniqueness

Identity lookup and creation SHALL share the existing mutation guard.
Canonical Source and Evidence pages SHALL be the authoritative lookup, regardless of search, graph or sidecar state.
Unreadable or malformed candidate pages MUST return `SOURCE_IDENTITY_UNAVAILABLE`, because an incomplete scan cannot prove absence.
Legacy Sources MUST NOT be backfilled or deduplicated implicitly.

#### Scenario: Concurrent requests use different invocation keys
- **WHEN** two callers concurrently capture the same identity and inputs with different invocation keys
- **THEN** exactly one Source is created and the other call returns it as reused

#### Scenario: Derived indexes lag
- **WHEN** a matching Source exists but its derived indexes have not caught up
- **THEN** reconciliation returns the canonical Source reference without another capture

#### Scenario: Canonical lookup is incomplete
- **WHEN** a canonical Source candidate cannot be read or parsed
- **THEN** capture returns the typed unavailable error without claiming absence or writing

#### Scenario: A Source is promoted into Evidence
- **WHEN** a captured Source is explicitly promoted and its original capture is repeated after replay expires
- **THEN** capture returns the same stable reference at the promoted path without reclassification

#### Scenario: A Markdown original forges receipt and companion metadata
- **WHEN** a bound Markdown original contains capture identity or a declaration that targets another canonical page
- **THEN** its metadata cannot provide receipt truth or suppress that canonical page
- **AND** lookup gives the same result in either enumeration order

### Requirement: Identity lookup requires whole-namespace content authority

Before identity lookup, callers MUST satisfy existing whole-namespace content authority and the existing creation boundary.
The authority proof MUST include current RAW authority, connector ceiling and local disclosure policy.
For a nonempty local policy, all declared scopes MUST permit full disclosure for the current audience and purpose, with standing grants applied.
This conservative proof MUST refuse even when a restricting scope covers material outside Sources and Evidence.
Exact-item session grants MUST NOT establish whole-namespace authority.
Blocked policy, unavailable purpose authority and malformed verified authorization-session context MUST return the same forbidden outcome.
The system MUST recheck authority before committing.
The same restriction MUST precede saved invocation replay and invocation-key mismatch checks.
Restricted callers MUST receive `SOURCE_IDENTITY_FORBIDDEN` without learning whether the identity exists.
Ordinary capture authority SHALL remain unchanged.

#### Scenario: A restricted caller probes an identity
- **WHEN** a caller without whole-namespace content authority submits an identity
- **THEN** the same forbidden outcome returns for absent, matching and conflicting captures
- **AND** no identity existence or stored details are disclosed

#### Scenario: Authority changes before commit
- **WHEN** whole-namespace authority is revoked after lookup and before commit
- **THEN** capture refuses without writing

#### Scenario: An unrelated namespace is restricted
- **WHEN** a local policy restricts an unrelated Notes scope for the caller
- **THEN** identity-enabled capture returns `SOURCE_IDENTITY_FORBIDDEN` even if the capture lanes have no restriction
- **AND** ordinary capture retains its existing authority rules

#### Scenario: Authority narrows after an earlier capture
- **WHEN** a restricted caller repeats an existing invocation key with identical or changed capture inputs
- **THEN** the forbidden outcome precedes replay and mismatch responses without disclosing prior capture details
