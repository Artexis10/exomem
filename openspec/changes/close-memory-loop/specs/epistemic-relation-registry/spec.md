## ADDED Requirements

### Requirement: An extension keeps its parent's core family

A governed relation extension MUST NOT declare a family that differs from its core parent's family; an extension that declares none SHALL inherit its parent's. A relation save or merge delta that adds an extension, or changes an extension's declared family, with a mismatching family SHALL be refused with a `family_mismatch` finding naming the offending key, so a declared family cannot present ownership, composition, supersession, duplication, contradiction or causality as a new meaning. An extension already present in the saved registry with a mismatching family SHALL be grandfathered: the registry reports a non-blocking `family_mismatch` warning, and saves that add unrelated extensions or deprecate the offender SHALL still succeed.

#### Scenario: A new extension declaring another family is refused
- **WHEN** a `save-relations` delta adds an extension parented on `owns` that declares the family `association`
- **THEN** the save is refused with a `family_mismatch` finding and the registry is unchanged

#### Scenario: A saved mismatch does not block unrelated saves
- **WHEN** the saved registry already holds an extension parented on `owns` with family `association`
- **THEN** loading the registry reports one `family_mismatch` warning, adding an unrelated extension succeeds, deprecating the offender succeeds, and adding a new mismatching extension is still refused

#### Scenario: An omitted family inherits the parent's
- **WHEN** an extension parented on `owns` declares no family
- **THEN** its family is the core family of `owns`
