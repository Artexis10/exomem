## ADDED Requirements

### Requirement: Session Validation Outage Tolerance

The hosted gateway SHALL maintain a per-replica durable cache of successful
session validations and MAY use a cached validation only when the remote session
store is unavailable and the entry is within the configured stale grace window.
The cache SHALL identify entries by a digest of the token, SHALL encrypt the
validated claims at rest, and SHALL never persist raw tokens or negative
validation results. Token issuance, exchange, refresh, dynamic client
registration, and revocation SHALL continue to require the live store.

#### Scenario: Store outage with a recently validated session

- **WHEN** the remote session store is unavailable
- **AND** the same replica previously validated the session successfully
- **AND** the cached validation is within `EXOMEM_SESSION_STALE_GRACE_SECONDS`
- **THEN** the cached claims are served
- **AND** `stale_served_count` is incremented
- **AND** `/health/ready` reports `session_store.state` as `degraded`

#### Scenario: Store outage without a prior validation

- **WHEN** the remote session store is unavailable
- **AND** no eligible successful validation exists in the local cache
- **THEN** the session is refused with `temporarily_unavailable`

#### Scenario: Authoritative revocation clears stale eligibility

- **WHEN** the remote session store authoritatively reports a session invalid,
  revoked, or expired
- **THEN** the local cache entry is deleted
- **AND** the session is refused
- **AND** that entry is never served stale on a later store outage

#### Scenario: Stale grace kill switch

- **WHEN** `EXOMEM_SESSION_STALE_GRACE_SECONDS=0`
- **AND** the remote session store is unavailable
- **THEN** validation fails closed exactly as it did before this change
- **AND** no cached validation is served

#### Scenario: Session token confidentiality on disk

- **WHEN** a successful validation is persisted locally
- **THEN** its sqlite key is `sha256(token)` rather than the raw token
- **AND** its claims value is encrypted at rest
- **AND** the raw token does not appear in the cache file
