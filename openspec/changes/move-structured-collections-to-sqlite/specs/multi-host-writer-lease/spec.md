## MODIFIED Requirements

### Requirement: Opt-in multi-host coordination

Exomem SHALL preserve its current single-host behavior unless writer-lease coordination is explicitly configured. When coordination is enabled, every replica SHALL use a stable vault identifier and a unique replica identifier when requesting authority to mutate the vault.

`EXOMEM_WRITER_LEASE_TOPOLOGY` SHALL select the closed deployment modes `multi-host` or `single-host`, with `multi-host` as the default. Explicit `single-host` SHALL require a configured credential-free literal loopback HTTP coordinator origin. Other URL components, hostname aliases, and non-loopback addresses SHALL refuse configuration. Its coordinator requests SHALL bypass proxies and SHALL NOT follow a redirect to another origin. The default topology SHALL retain existing multi-host authentication requirements.

Single-host coordination SHALL preserve the existing local session authority, FastMCP OAuth storage, signing key, and identity when no shared OAuth storage URL is configured. An explicit shared OAuth storage URL SHALL retain its existing selection and validation. Doctor SHALL retain applicable lease configuration and coordinator checks while omitting HA edge and two-replica checks for single-host. It SHALL require shared-auth checks only when shared OAuth storage is configured in that topology. Invalid configuration SHALL remain visible as a failure. This topology SHALL reuse the existing lease, fence, and store-head protocol without changing governance, deadlines, or supported-reader admission.

Doctor SHALL verify coordinator bearer enforcement through authenticated and credential-free native lease status requests.

#### Scenario: Existing single-host installation

- **WHEN** no writer-lease coordinator is configured
- **THEN** reads and writes behave as they did before this capability was added

#### Scenario: Coordinated replica starts

- **WHEN** a coordinator URL, vault identifier, and replica identifier are configured
- **THEN** the replica participates in writer-lease coordination for that vault

#### Scenario: Existing local sessions survive single-host coordination

- **WHEN** one managed service enables single-host coordination on a literal loopback origin and rebuilds its authentication provider
- **THEN** its existing durable sessions and OAuth client registrations remain usable in their original storage
- **AND** existing revocation and identity checks remain in force

#### Scenario: Single-host cannot silently become remote authority

- **WHEN** single-host configuration supplies a remote or credential-bearing origin, or the coordinator redirects to another origin
- **THEN** configuration or the coordinator request refuses before contacting that other authority

#### Scenario: Single-host doctor needs no HA deployment

- **WHEN** doctor probes a valid single-host deployment without shared OAuth storage
- **THEN** it checks the local coordinator and lease settings without requiring an HA edge, shared auth service, or two replica origins
- **AND** authenticated lease status succeeds and credential-free lease status returns native HTTP 401
- **AND** anonymous success, another HTTP status, or unavailable transport fails the diagnostic visibly
