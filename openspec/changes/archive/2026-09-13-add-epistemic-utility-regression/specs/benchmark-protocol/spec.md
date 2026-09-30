## ADDED Requirements

### Requirement: Utility execution enforces the registered family's release

The utility action episode SHALL be registered as a new operational family in the epistemic preregistration table and registry, with deterministic action-state and prohibited-effect assertions. Its amendment SHALL use the existing receipt chain and mirrored family map. Both the utility runner and comparative report reader SHALL explicitly enforce `require_amended_families_released` with the registered family. Identity derivation alone SHALL NOT be treated as authorization to execute or claim utility. Other track scores and denominator rules SHALL remain unchanged.

#### Scenario: Utility receipt remains pending

- **WHEN** the utility family's amendment exists with acknowledgment pending
- **THEN** comparative execution and utility claims refuse with the existing typed error
- **AND** unrelated released families and labelled model-free instrument tests remain available

#### Scenario: Utility receipt is acknowledged

- **WHEN** the utility receipt is acknowledged and its chain, registry mapping and run identity agree
- **THEN** the release gate permits that family to proceed through the separate execution and spend gates
