## ADDED Requirements

### Requirement: Catalog predecessor identity matches on every declared platform

A v4 catalog generation SHALL match each mutation to its reviewed predecessor by
an identity that is stable across the platforms the package declares support for,
so that the same governed write publishes on each of them or refuses on all of
them for the same reason. A refusal SHALL distinguish a predecessor that is
absent at the requested identity from one whose content hash differs, because
those name different defects. Matching SHALL NOT be relaxed in a way that lets
two distinct membership targets resolve to one predecessor.

#### Scenario: The same governed write publishes on every declared platform

- **WHEN** a governed semantic write or removal publishes a catalog generation on any platform the package declares
- **THEN** the mutation matches its reviewed predecessor
- **AND** the outcome does not depend on how that platform spells the path

#### Scenario: A refusal names which identity check failed

- **WHEN** catalog publication refuses a mutation
- **THEN** the refusal distinguishes an absent predecessor from a content-hash mismatch

#### Scenario: Colliding membership targets are still refused

- **WHEN** two distinct mutation targets normalize to one membership identity
- **THEN** publication refuses the batch rather than matching both to one predecessor
