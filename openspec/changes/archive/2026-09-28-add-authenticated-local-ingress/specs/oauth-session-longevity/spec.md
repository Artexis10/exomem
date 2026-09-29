## ADDED Requirements

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
