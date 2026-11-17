## ADDED Requirements

### Requirement: Categories Carry Registry-Declared Traits
A category definition in the shipped pack or in the vault's semantic-language registry SHALL be able to declare `traits` from the same closed trait set that kinds use. The closed set SHALL include `position`, meaning that a unit records the person's own standing position: an attribution, a preference, an interpretation, a working model or a constraint on framing. A declaration SHALL take effect only after a reviewed registry save. A declaration that names a trait outside the closed set SHALL fail validation with a named finding. A declaration SHALL NOT change `category_key` or unit identity.

#### Scenario: A vault declares a position category
- **WHEN** a reviewed save of the vault registry adds category `interpretation_preference` with `traits: [position]`
- **THEN** units of that category, and of every category aliased to it, carry the trait `position`, and their keys and identities do not change

#### Scenario: An unknown trait is refused
- **WHEN** a registry save declares a category with trait `important`
- **THEN** validation fails with a finding that names the trait and the closed set, and nothing changes
