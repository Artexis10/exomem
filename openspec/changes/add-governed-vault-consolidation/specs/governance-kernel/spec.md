## ADDED Requirements

### Requirement: Protective restore continuity does not replace admission membership

A destination-owned stopped protective restore MAY use versioned complete archive continuity evidence for its exact Scope proposal. Only the internally validated restore binding SHALL select this evidence. Proposal creation, legacy and v4 commit validation, and exact recovery SHALL use one evidence selector. Current authority, exact missing documents, policy compilation, and existing selector conflicts SHALL remain enforced. Public claims of a restore discriminator or digest SHALL NOT supply authority.

Continuity SHALL NOT substitute for admission classification, grant drift, or dependent grant membership. Membership consequences SHALL be marked unevaluated. Any conservative transition direction SHALL state that it does not measure disclosure change. Ordinary membership proposals and persisted legacy proposals SHALL retain their evidence meaning.

#### Scenario: Complete restored bytes have unresolved admission membership

- **WHEN** an exact stopped restore verifies all archive bytes while installing destination protective Scopes
- **THEN** proposal evidence proves archive continuity without inferring empty membership for unresolved configuration
- **AND** restricted reads and capture namespace checks continue to refuse that unresolved configuration

#### Scenario: A dependent grant needs unresolved membership

- **WHEN** a protective proposal affects a grant whose targets lack resolved classification
- **THEN** the canonical grant validation remains unavailable
- **AND** complete archive continuity cannot supply permitted targets

#### Scenario: Persisted continuity evidence is changed

- **WHEN** exact recovery finds a different inventory, manifest, destination, placement, operation, or protective document binding
- **THEN** commit refuses before recognizing a spent proposal as complete
- **AND** a public caller cannot select continuity evidence without the validated stopped restore binding
