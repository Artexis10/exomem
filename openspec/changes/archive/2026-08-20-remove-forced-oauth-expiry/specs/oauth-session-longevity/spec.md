## ADDED Requirements

### Requirement: OAuth session lifetime follows provider capabilities
The system SHALL construct the remote OAuth proxy without imposing an Exomem-specific fallback access-token expiry, allowing the OAuth implementation to select lifetime and refresh behavior from the upstream token response.

#### Scenario: GitHub OAuth token has no refresh metadata
- **WHEN** the upstream GitHub OAuth token response omits `expires_in` and `refresh_token`
- **THEN** Exomem does not force the downstream connection to expire after 30 days
- **AND** the OAuth implementation's no-refresh fallback policy applies

#### Scenario: Upstream provider supplies expiry or refresh metadata
- **WHEN** the upstream token response supplies an expiry or refresh token
- **THEN** the OAuth implementation uses that provider metadata without an Exomem fallback overriding it

### Requirement: Existing authentication safeguards remain intact
The system SHALL preserve the configured GitHub account verifier, stable JWT signing key, and optional shared OAuth storage when removing the forced expiry.

#### Scenario: Authenticated remote server is constructed
- **WHEN** required GitHub and Exomem authentication settings are present
- **THEN** the proxy retains the single-user verifier and existing signing/storage configuration
- **AND** only the fixed fallback-expiry override is absent
