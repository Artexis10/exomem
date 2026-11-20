## ADDED Requirements

### Requirement: Explicit incarnation erasure closes old capture replay

Owner-enabled erasure SHALL be an explicit exception to ordinary Source preservation. Old capture terminals MUST NOT replay a retired reference as a live Source. Fresh capture of its logical identity SHALL allocate a fresh reference. Erasure MUST NOT infer a correction or keep a permanent logical-identity tombstone.

#### Scenario: Retired capture is retried
- **WHEN** a caller repeats an earlier capture after that Source incarnation completes erasure
- **THEN** a saved terminal cannot present the retired Source as live
- **AND** a fresh admitted capture can create the logical identity with a new Source reference

### Requirement: Capture settlement distinguishes admission from custody

Settlement SHALL preserve its scoped admission and past-outcome guarantees while reporting custody completeness, owned allocations and attempt residue separately. Ordinary settlement MUST NOT destroy an uncertain capture's preserved original. Explicit cleanup SHALL retain unresolved custody beyond normal retry-cache expiry.

#### Scenario: Capture outcome is unknown but allocation is known
- **WHEN** a capture loses its terminal after allocating a Source reference
- **THEN** settlement can report unknown past outcome and the proved owned allocation without claiming publication or absence

#### Scenario: Ordinary settlement reports residue
- **WHEN** a caller settles an interrupted attempt without requesting cleanup
- **THEN** admission closes within its proved scope and retained original bytes remain available for recovery
