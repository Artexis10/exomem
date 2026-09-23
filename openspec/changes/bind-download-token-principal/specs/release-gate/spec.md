## ADDED Requirements

### Requirement: Out-of-band download capabilities carry their minting principal

A download capability minted by `transfer_artifact(operation="download")` SHALL carry the canonical audience of the principal that minted it, signed together with the capability's scope and expiry under the transfer secret, so that the holder cannot alter it. `/download` SHALL resolve a presented capability to that audience, not to the owner, and SHALL decide the requested path under it through the same full-disclosure release decision other binary egress uses. Only a capability minted by the owner SHALL resolve to the owner, and the long-lived transfer secret itself SHALL remain the owner's credential. A minting caller whose principal is unresolved, or who has no principal bound, SHALL bind the most-restrictive audience.

A capability SHALL carry standing authority only. It MUST NOT carry the minting call's declared purpose or authorization session, because an out-of-band bearer is a move between issuer families that canonical-principal equivalence alone does not authorize.

A capability SHALL NOT be bound to a path. The tool takes no path, and clients choose the path at download time, so audience binding is the contract-compatible bound: it limits the capability to what its minter may read in full.

A download capability without an audience claim, including one minted before this binding existed, SHALL be refused as an unauthenticated download rather than resolved to any principal.

A path withheld from the resolved audience and a path that does not exist SHALL receive one byte-identical refusal, whose reason is spelled from the request and not from the on-disk spelling the path resolver returns.

#### Scenario: A non-owner's capability is decided as its minter

- **WHEN** an OAuth principal or a Cloudflare Access principal mints a download capability and requests a path withheld from it
- **THEN** `/download` refuses the path exactly as it refuses a missing one
- **AND** a path released to that principal at full disclosure downloads with the same capability

#### Scenario: The owner keeps the owner's view

- **WHEN** the owner mints a download capability, or presents the transfer secret itself
- **THEN** `/download` resolves the owner and releases what the owner may read in full

#### Scenario: An unresolved or unbound minter binds the floor

- **WHEN** a capability is minted by a caller whose principal is unresolved, or with no principal bound
- **THEN** `/download` resolves it to the most-restrictive audience, never to the owner

#### Scenario: The bound audience cannot be swapped or extended

- **WHEN** a presented capability's audience claim, expiry, or signature has been altered, or the capability has expired
- **THEN** it does not verify and the download is refused as unauthenticated

#### Scenario: A claimless capability is refused

- **WHEN** a download capability carrying no audience claim is presented
- **THEN** `/download` refuses it as unauthenticated and does not resolve it to the owner

#### Scenario: Withheld and missing are indistinguishable

- **WHEN** the same requested spelling is refused once while its target exists but is withheld, and once after the target is removed, including on a filesystem that re-spells an existing path to its on-disk casing
- **THEN** both refusals have the same status and byte-identical bodies, and neither reveals the on-disk spelling
