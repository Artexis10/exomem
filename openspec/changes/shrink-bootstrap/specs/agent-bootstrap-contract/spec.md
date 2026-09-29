## ADDED Requirements

### Requirement: Compact bootstrap serves a core and on-demand sections

For every surface whose bootstrap contract is not published as a frozen hosted profile, `bootstrap` with `profile="compact"` SHALL return a core operating contract and an index of named sections, not the full reference payload. The core SHALL carry every rule named in the core rule manifest at every engagement level the manifest assigns it to. Each section SHALL be retrievable through `bootstrap(profile="compact", section=<name>)` and SHALL contain the blocks it names byte-identically to their pre-core form. `section="index"` SHALL list every section with its size in served-JSON bytes. An unknown section SHALL fail with a validation error naming the accepted sections. The `full`, `diagnostics` and `session` profiles SHALL be unchanged.

#### Scenario: The core carries the incident-preventing rules at every level that owes them

- **WHEN** compact bootstrap is served at any engagement level on any non-frozen surface
- **THEN** it carries the recall-before-answering rule at `balanced` and `maximal`, the capture loop, the episode-recording pass at `balanced` and `maximal`, the epistemic commitments, the delegation ceiling, the due-state restraint and the rule that a withheld result is indistinguishable from an absent one

#### Scenario: A section returns what compact used to return

- **WHEN** a client calls `bootstrap(profile="compact", section="authoring")`
- **THEN** the response carries the authoring blocks byte-identically to the pre-core compact payload
- **AND** the union of the core and every section contains every pre-core leaf except those the manifest lists as replaced by a digest, each of which names the section holding the original

#### Scenario: An unknown section is refused

- **WHEN** a client calls `bootstrap(section="nonexistent")`
- **THEN** the operation fails with a validation error naming the accepted sections

### Requirement: Compact core stays under a derived byte budget

The compact core SHALL stay at or under its ruled byte ceiling at every engagement level on every non-frozen surface, including the hook-capable worst case, with the vault-derived blocks at their maximum bound. The ceiling SHALL be derived from the core allocation with a documented margin, not from the size the payload happens to have. Every section SHALL also stay at or under its own ceiling. Raising any ceiling SHALL require an entry in the core rule manifest or an argument recorded beside the constant that the added bytes are needed on every session.

#### Scenario: A new block cannot grow the core silently

- **WHEN** a change adds bytes to the core past its headroom warning
- **THEN** the budget test warns before the ceiling and fails at it
- **AND** the change must name the manifest rule the bytes carry

### Requirement: Released hosted profiles keep their bootstrap payload

`hosted-alpha-agent-v1` through `hosted-alpha-agent-v4` SHALL serve the same compact bootstrap payload after the core split as before it, at every engagement level, and SHALL keep their pinned `bootstrap` parameter schema without a `section` argument. The released hosted plugin candidates and their compatibility descriptors for those profiles SHALL NOT change.

#### Scenario: A frozen profile is byte-stable across the split

- **WHEN** compact bootstrap is served under a frozen hosted profile at any level
- **THEN** its digest, with only the server version and tool-surface digests normalised, equals the digest recorded before the split
