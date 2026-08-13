## ADDED Requirements

### Requirement: Records authoring is self-describing and safely preflightable

The Records product command SHALL expose content-free `describe` and `validate` actions in addition to collection inspection and mutation. `describe` SHALL return the complete supported manifest contract, all closed enum values, exact open constraints, and generic minimal and nested-measurement examples. `validate` SHALL run the binding parser, Records-profile rule, safe-path rules, create-only checks, and scaffold checks without requiring a mutation reason, acquiring writer authority, writing an audit event, or changing the vault.

#### Scenario: Generic client creates the first collection without guessing

- **GIVEN** an empty sample vault and a client with no repository, skill, or fixture-manifest access
- **WHEN** the client calls `describe`, authors a manifest only from that response, and calls `validate`
- **THEN** validation succeeds without any guessed field name or enum value
- **AND** the same manifest can be created, inspected, and appended through the public command

#### Scenario: Laboratory example teaches nested observed measurements

- **WHEN** a client requests the Records authoring contract
- **THEN** the complete example shows a panel date, provenance link, and child analytes with values, inequalities, units, ranges, cancellation, and specimen qualifiers
- **AND** it contains no diagnosis, interpretation, private identity, or domain-specific storage engine

#### Scenario: Validation performs no mutation

- **WHEN** a valid or invalid manifest is submitted to `validate`
- **THEN** no manifest, source, directory scaffold, activity event, governance receipt, or writer-lease mutation is produced

### Requirement: Records inventory is available before a selector is known

Calling `record_memory(action="inspect")` without a collection SHALL return a bounded, governance-filtered inventory of releasable first-class Records manifests and exact Records-layer legacy trackers. Supplying a collection SHALL preserve targeted inspection behavior. Inventory SHALL NOT parse legacy item grammar or return item contents.

#### Scenario: Empty vault inventory is useful and empty

- **WHEN** a generic client inspects an empty Records layer without a collection selector
- **THEN** it receives empty first-class and legacy inventories plus the route to `describe`

#### Scenario: Denied inventory candidate stays absent

- **WHEN** governance withholds a first-class manifest or legacy tracker
- **THEN** inventory does not reveal its path, title, identity, type, or existence
