## RENAMED Requirements

- FROM: `### Requirement: Local tokens are issued, listed and revoked by the operator`
- TO: `### Requirement: Local tokens are issued, listed and revoked on the owner's machine`

## MODIFIED Requirements

### Requirement: Local tokens are issued, listed and revoked on the owner's machine

`exomem auth issue-local --client <name> --output <file>` SHALL mint a non-expiring local
session for the configured owner identity, create the file exclusively with mode 0600 and
never print or log the token. A failure to write the file SHALL revoke the new session.
`exomem auth sessions` SHALL list local sessions and name their ingress, `exomem auth revoke
<id>` SHALL end one, and `exomem auth revoke --all` SHALL end them with every other session.

#### Scenario: Issue a token
- **WHEN** a process running as the owner's user with the service configuration issues a token for client label `home`
- **THEN** a 0600 file holds it, the command prints only its session id, client label and output path, and the next local request with it is accepted

#### Scenario: Revoke a token
- **WHEN** that session is revoked, or all sessions are revoked
- **THEN** the next local request with the token receives a 401
