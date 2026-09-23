## MODIFIED Requirements

### Requirement: Canonical audience resolution, threaded and fail-closed

Every content-returning read SHALL resolve a canonical principal at its surface boundary
— MCP OAuth principal, REST key/identity scope, hosted gateway principal, explicit
local owner for stdio/CLI, or the owner for a verified remote OAuth principal the host
has explicitly bound as owner-equivalent — normalized into one comparable audience space, and SHALL
thread it to the release decision. A standing policy grant authored against one surface
SHALL match the same canonical principal on another surface.

Session-scoped authority SHALL additionally require a verified server-issued
authorization-session capability bound to that canonical principal, its trusted issuer/
surface family, and expiry. Canonical-principal equivalence alone MUST NOT move an
ephemeral grant, purpose, token, or revocation between issuer families or authorization
sessions. When identity or required session capability should resolve but cannot, the
release decision SHALL fail closed to the most restrictive outcome, never to full
disclosure or an implicit owner. Owner equivalence for a remote principal SHALL arise
only from the host's explicit binding and SHALL keep that principal's remote issuer
family.

For every session-aware content route—find/search/ask, get/fetch/read, browse/list,
graph/link suggestions, review/audit/provenance, Records/Planning, dataset, media/frame,
retrieve/inject hooks, and content-bearing writer results—the protected credential SHALL
be optional with absent meaning standing-only, present-valid installing the session, and
present-invalid rejecting the complete request. Extraction/redaction, trusted principal
resolution, and capability verification SHALL happen before ordinary validation, cache
key/lookup, idempotency lookup, membership/decision, or content/state work. No route may
silently ignore an invalid credential or reuse a standing-only cached decision for a
verified session.

#### Scenario: Same principal across surfaces keeps standing authority

- **WHEN** the same human queries through two surfaces that normalize to one canonical
  principal
- **THEN** standing rules and standing grants authored for that principal apply on both

#### Scenario: Session authority also requires its issuer binding

- **WHEN** the same canonical principal has a session grant in issuer family A and calls
  through issuer family B without opening a B session
- **THEN** the standing policy still applies but A's session grant, purpose, and tokens do
  not

#### Scenario: Verified session resumes across transport reconnect

- **WHEN** the caller presents the valid capability after reconnect or replica routing
  within the bound issuer family
- **THEN** the same internal session state participates without using process-local
  transport identity

#### Scenario: Unresolved identity denies

- **WHEN** an authenticated surface cannot resolve the expected principal
- **THEN** the decision is most-restrictive, not OPEN

#### Scenario: Unresolved required session does not fall back

- **WHEN** a request attempts session-scoped grant or purpose authority without a valid
  principal/issuer/session binding
- **THEN** that authority is absent and the operation fails closed rather than treating
  the caller as owner

#### Scenario: Invalid optional credential does not degrade to standing policy

- **WHEN** a caller presents an invalid credential to any session-aware content route
  that could otherwise return standing-policy content
- **THEN** the whole request receives the common credential refusal before validation,
  cache, idempotency, membership, decision, or content work

#### Scenario: Same principal across surfaces

- **WHEN** the same human queries via MCP and via REST
- **THEN** a grant authored for that principal applies on both

#### Scenario: An explicitly bound remote identity resolves to the owner

- **WHEN** the host has bound the remote OAuth identity as owner-equivalent and that
  identity calls the MCP connector
- **THEN** the release decision uses the owner audience, and session authority stays
  bound to the remote issuer family

#### Scenario: An unbound remote identity stays a distinct principal

- **WHEN** no binding names the remote OAuth identity
- **THEN** it resolves to its own canonical principal and never to the owner
