## ADDED Requirements

### Requirement: The binding manifest contract is machine-discoverable

The collection substrate SHALL project a deterministic versioned manifest JSON Schema derived from the same constants and constraints enforced by the binding parser. The projection SHALL distinguish closed enums from open strings, include required fields and nested field grammar, and contain canonical minimal and complete examples that parse successfully.

#### Scenario: Closed enums stay aligned with validation

- **WHEN** supported profiles, collection versions, storage strategies, storage format versions, field types, saved-view operators, or aggregates change
- **THEN** the described contract and actionable validation details expose the same values
- **AND** parity tests fail if parser and contract diverge

#### Scenario: Lifecycle is described honestly

- **WHEN** the contract describes `lifecycle`
- **THEN** it identifies a required non-empty string constraint and an `active` example
- **AND** it does not invent a closed lifecycle enum that the parser does not enforce

### Requirement: Closed manifest validation failures are self-remediating

Manifest validation errors SHALL preserve their stable code and message while adding bounded machine-readable remediation facts. Closed enum errors SHALL name the exact field, received value, allowed values, and a minimal example. Missing required fields SHALL name the field, expected shape, and minimal example.

#### Scenario: Unsupported profile returns the allowed profiles

- **WHEN** a proposed manifest declares an unsupported semantic profile
- **THEN** the error retains `UNSUPPORTED_COLLECTION_PROFILE`
- **AND** it identifies `semantic_profile`, the received value, the complete allowed set, and `semantic_profile: records` as an example

#### Scenario: Missing collection version names the exact field

- **WHEN** a proposed manifest omits `collection_version`
- **THEN** the error retains `UNSUPPORTED_COLLECTION_VERSION`
- **AND** it identifies the missing field, allowed versions, and `collection_version: 1` as an example
