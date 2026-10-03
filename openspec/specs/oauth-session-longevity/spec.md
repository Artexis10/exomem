# oauth-session-longevity Specification

## Purpose
Let OAuth session lifetime follow the provider's capabilities by removing the Exomem-specific forced access-token expiry, while keeping the GitHub account verifier, signing key and shared storage. The public path continues to reject local-ingress sessions.

## Requirements

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

### Requirement: The public path rejects local-ingress sessions

The public OAuth session authority SHALL continue to refuse any session whose issuer or
audience is not its own, so a local-ingress session SHALL never authenticate a request on
the public path, and issuing local sessions SHALL NOT change how public sessions are
issued, refreshed, validated or revoked.

#### Scenario: A local token presented on the public path
- **WHEN** a request on the public path presents a valid local-ingress token
- **THEN** it receives the ordinary 401 with the OAuth `resource_metadata` challenge

#### Scenario: Revoke all ends both kinds
- **WHEN** the operator runs `exomem auth revoke --all`
- **THEN** public and local sessions both stop validating
