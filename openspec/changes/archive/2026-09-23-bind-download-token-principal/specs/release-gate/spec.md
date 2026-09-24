## ADDED Requirements

### Requirement: Out-of-band download capabilities carry their minting principal

A download capability minted by `transfer_artifact(operation="download")` SHALL carry the canonical audience of the principal that minted it, signed together with the capability's scope and expiry under the transfer secret, so that the holder cannot alter it. `/download` SHALL resolve a presented capability to that audience, not to the owner, and SHALL decide the requested path under it through the same full-disclosure release decision other binary egress uses. Only a capability minted by the owner SHALL resolve to the owner, and the long-lived transfer secret itself SHALL remain the owner's credential. A minting caller whose principal is unresolved, or who has no principal bound, SHALL bind the most-restrictive audience.

A capability SHALL carry standing authority only. It MUST NOT carry the minting call's declared purpose or authorization session, because an out-of-band bearer is a move between issuer families that canonical-principal equivalence alone does not authorize.

A capability SHALL NOT be bound to a path. The tool takes no path, and clients choose the path at download time, so audience binding is the contract-compatible bound: it limits the capability to what its minter may read in full.

A download capability without an audience claim, including one minted before this binding existed, SHALL be refused as an unauthenticated download rather than resolved to any principal.

A path withheld from the resolved audience, a folder, and a path that does not exist SHALL receive one byte-identical refusal, whose reason is spelled from the request and not from the on-disk spelling the path resolver returns. A path the resolver refuses as outside the vault SHALL be refused with a fixed reason that names no server path, and a malformed bearer SHALL be refused as unauthenticated, never answered with a server error.

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

#### Scenario: A folder answers like a missing path

- **WHEN** a folder is requested, including one inside a withheld scope
- **THEN** `/download` answers with the same status and body it gives the same spelling once the folder is gone

#### Scenario: An escaping path names no server path

- **WHEN** a requested path traverses out of the vault or through a symlink whose target lies outside it
- **THEN** the refusal carries a fixed reason and neither the absolute server path nor the link target

### Requirement: Exact unit reads and graph seeds follow the page release decision

An exact semantic-unit read SHALL take the page release decision a page read takes before it resolves any unit reference, because a unit, its parent citation and its surrounding Markdown are the page's own contents and whether a reference resolves is a fact about the page. A page withheld from the caller SHALL answer an exact unit read byte-identically to an absent page, for every unit reference. A unit SHALL be served only from a page released to the caller in full, with its parent context cut from the released body and its parent citation built from the released frontmatter; below full release it SHALL answer as an absent page.

A graph-context seed SHALL be decided the same way. A unit reference whose parent page is released to the caller below the level at which the graph withholds a seed SHALL resolve, validate and report drift as a unit of an absent page, while the response still echoes the caller's own reference. A context seeded from a page withheld from the caller SHALL answer as for an absent page.

#### Scenario: Exact unit read of a withheld page

- **WHEN** a caller the page is withheld from reads it with a real unit reference, with an unknown anchor on the same page, or with an unrelated reference
- **THEN** every answer is byte-identical to the answer for the same request against an absent page, on every surface

#### Scenario: Exact unit read below full release

- **WHEN** the page is released to the caller at notice, abstract or excerpt level
- **THEN** no unit is served and the answer is the absent page's

#### Scenario: Graph seed of a withheld page

- **WHEN** a context or graph-context request is seeded with a page path, or with a unit reference that names its page by memory id or by path, and that page is withheld from the caller
- **THEN** the answer is the one the same request receives when the page is absent, with no resolution status or drift that exists only because the page does

#### Scenario: The owner's seed answer does not depend on the graph's row budget

- **WHEN** the owner seeds a context from a unit reference while a policy exists and the graph's index holds more parent rows for that reference than the resolver examines
- **THEN** the stale-index drift and budget report the owner would receive with no policy are not replaced by a withheld answer, even though a rule that names the owner audience still decides the owner's candidates the same way it decides any other caller's
